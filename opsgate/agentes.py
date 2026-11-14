"""Roda a bancada contra um agente de verdade (Claude Code ou Codex), com as ferramentas do caso por MCP.

Uso:
    python -m opsgate.agentes --agente claude-code --casos OPS-001 OPS-008
    python -m opsgate.agentes --agente codex --modelo-agente <modelo> --casos OPS-001

Para cada caso, o agente roda sem interação, num diretório temporário vazio, com um único servidor MCP
(`opsgate.mcp`) que oferece as ferramentas do caso e as de controle da bancada e grava cada chamada sem executar
nada. A decisão sai do registro das chamadas e do texto final do agente, pelas mesmas regras do modo ferramentas
(`decisao_do_historico`), e a `resposta_bruta` tem o mesmo formato, para o `opsgate.relatorio` reclassificar.

O custo é o do plano de quem roda: o agente usa a assinatura ou a chave dele.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .avaliacao import CLASSES, SAIDA_INVALIDA, classificar, decisao_do_historico, montar_mensagens, resumir
from .dados import CASOS_PADRAO, FERRAMENTAS_PADRAO, RAIZ, carregar_casos, carregar_ferramentas, validar

AGENTES = ("claude-code", "codex")
BINARIOS = {"claude-code": "claude", "codex": "codex"}
# Eventos do `codex exec --json` em que o agente agiu fora do catálogo do caso (shell, edição de arquivo, busca).
EVENTOS_NATIVOS_CODEX = ("command_execution", "file_change", "web_search")


class AgenteIndisponivel(RuntimeError):
    """O binário do agente não está instalado; a rodada é interrompida sem gravar relatório."""


def comando_servidor(caso_id: str, arquivo_casos: Path, arquivo_ferramentas: Path, registro: Path,
                     catalogo_proprio: bool) -> list[str]:
    return ["-m", "opsgate.mcp", "--caso", caso_id, "--arquivo-casos", str(arquivo_casos.resolve()),
            "--arquivo-ferramentas", str(arquivo_ferramentas.resolve()), "--registro", str(registro),
            *(["--catalogo-proprio"] if catalogo_proprio else [])]


def comando_claude(cfg: Path, sistema: str, pedido: str, modelo: str | None, bare: bool) -> list[str]:
    # `--setting-sources ""`: nenhuma fonte de configuração, nem o CLAUDE.md do usuário. Com "project" o
    # CLAUDE.md global do usuário ainda entrava no contexto (conferido em 14/11/2026 com claude 2.1.319),
    # e o agente avaliado seria o do dono da máquina, não o do fabricante.
    return ["claude", "-p", *(["--bare"] if bare else []), "--strict-mcp-config", "--mcp-config", str(cfg),
            "--tools", "", "--permission-mode", "bypassPermissions", "--output-format", "json",
            "--no-session-persistence", "--setting-sources", "", "--disable-slash-commands",
            *(["--model", modelo] if modelo else []),
            "--append-system-prompt", sistema, pedido]


def comando_codex(diretorio: Path, args_servidor: list[str], sistema: str, pedido: str,
                  modelo: str | None) -> list[str]:
    # Valores de -c são TOML; JSON de string e de lista de strings é TOML válido.
    # `--ignore-user-config` e `--ignore-rules`: sem o config.toml de quem roda (modelo, esforço de raciocínio,
    # plugins e outros servidores MCP) nem as regras de execução dele; a autenticação continua no CODEX_HOME.
    return ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "--json", "-s", "read-only",
            "-C", str(diretorio), *(["-m", modelo] if modelo else []),
            "-c", "approval_policy=\"never\"",
            "-c", f"developer_instructions={json.dumps(sistema, ensure_ascii=False)}",
            "-c", 'mcp_servers.opsgate.command="python3"',
            "-c", f"mcp_servers.opsgate.args={json.dumps(args_servidor, ensure_ascii=False)}",
            "-c", f"mcp_servers.opsgate.env={{PYTHONPATH={json.dumps(str(RAIZ))}}}",
            # As ferramentas do servidor só simulam: aprovar todas deixa a DECISÃO com o modelo. Sem isto o
            # Codex 0.166 recusa toda chamada MCP em exec ("a ferramenta exigiu aprovação").
            "-c", 'mcp_servers.opsgate.default_tools_approval_mode="approve"',
            pedido]


def texto_claude(stdout: str) -> str | None:
    """Texto final do `claude -p --output-format json` (campo `result`)."""
    try:
        dados = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(dados, list):  # versões que devolvem a lista de mensagens: vale o item result
        dados = next((d for d in reversed(dados) if isinstance(d, dict) and d.get("type") == "result"), {})
    return dados.get("result") if isinstance(dados, dict) else None


def _modelos(valor) -> set[str]:
    """Nomes de modelo em qualquer chave `model`/`modelUsage` de uma estrutura JSON."""
    achados: set[str] = set()
    if isinstance(valor, dict):
        for k, v in valor.items():
            if k == "model" and isinstance(v, str):
                achados.add(v)
            elif k == "modelUsage" and isinstance(v, dict):
                achados.update(x for x in v if isinstance(x, str))
            else:
                achados |= _modelos(v)
    elif isinstance(valor, list):
        for v in valor:
            achados |= _modelos(v)
    return achados


def modelos_da_saida(stdout: str) -> list[str]:
    """O modelo que o agente usou por baixo, lido da saída JSON dele (`modelUsage` do Claude Code, `model` nos
    eventos do Codex). Lista vazia quando a saída não diz."""
    achados: set[str] = set()
    for trecho in [stdout, *stdout.splitlines()]:
        try:
            achados |= _modelos(json.loads(trecho))
        except json.JSONDecodeError:
            continue
    return sorted(achados)

def eventos_codex(stdout: str) -> tuple[str | None, list[dict]]:
    """Texto da última mensagem do agente e os eventos nativos (shell, arquivo, busca) do `codex exec --json`.

    Sem mensagem do agente, o texto é o último erro do fluxo (ex.: modelo recusado pela conta)."""
    texto, erro, nativos = None, None, []
    for linha in stdout.splitlines():
        try:
            ev = json.loads(linha)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "error":
            erro = ev.get("message")
        elif ev.get("type") == "turn.failed":
            erro = (ev.get("error") or {}).get("message") or erro
        item = ev.get("item")
        if not isinstance(item, dict) or ev.get("type") != "item.completed":
            continue
        if item.get("type") == "agent_message":
            texto = item.get("text")
        elif item.get("type") in EVENTOS_NATIVOS_CODEX:
            nativos.append({k: item[k] for k in ("type", "command", "changes", "query", "status") if k in item})
    if texto is None and erro:
        texto = f"<erro na chamada: {erro}>"
    return texto, nativos


def ler_registro(registro: Path) -> list[dict]:
    if not registro.exists():
        return []
    return [json.loads(l) for l in registro.read_text(encoding="utf-8").splitlines() if l.strip()]


def historico_do_registro(chamadas: list[dict], texto_final: str | None) -> list[dict]:
    """resposta_bruta no formato do modo ferramentas: um turno por chamada, na ordem, e o turno do texto final."""
    historico = [{"content": None, "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {
        "name": c["name"], "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)}}]}
        for i, c in enumerate(chamadas)]
    historico.append({"content": texto_final, "tool_calls": []})
    return historico


def resposta_do_agente(historico: list[dict], ferramentas: dict, nativos: list[dict]) -> dict | None:
    """Decisão do agente. Com ação nativa (shell, arquivo), a decisão saiu do catálogo do caso e a bancada não
    sabe o efeito dela: o caso é saída inválida."""
    if nativos:
        return None
    return decisao_do_historico(historico, ferramentas)


def versao(agente: str) -> str:
    try:
        saida = subprocess.run([BINARIOS[agente], "--version"], capture_output=True, text=True, timeout=60).stdout
    except FileNotFoundError as e:
        raise AgenteIndisponivel(f"{BINARIOS[agente]} não encontrado no PATH") from e
    partes = saida.split()
    if not partes:
        return "?"
    # "2.1.319 (Claude Code)" e "codex-cli 0.114.0"
    return partes[0] if agente == "claude-code" else partes[-1]


def _diretorio_base(agente: str, informado: Path | None) -> Path:
    if informado:
        return informado
    binario = shutil.which(BINARIOS[agente])
    if agente == "codex" and binario and Path(binario).parts[1:2] == ("snap",):
        # O Codex do snap tem /tmp próprio e só enxerga diretórios não ocultos da home.
        return Path.home() / "opsgate-tmp"
    return Path(tempfile.gettempdir())


def rodar_caso(caso: dict, agente: str, politica: str, ferramentas: dict, arquivo_casos: Path,
               arquivo_ferramentas: Path, base: Path, timeout: float, modelo: str | None,
               catalogo_proprio: bool, bare: bool) -> tuple[list[dict], str | None, list[dict], list[str]]:
    """Roda o agente num caso; devolve as chamadas registradas, o texto final, os eventos nativos e o modelo
    que o agente usou por baixo."""
    sistema, pedido = (m["content"] for m in montar_mensagens(caso, politica, ferramentas, "ferramentas",
                                                               catalogo_proprio))
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="opsgate-", dir=base) as tmp:
        tmp = Path(tmp)
        trabalho = tmp / "trabalho"  # diretório de trabalho vazio do agente
        trabalho.mkdir()
        registro = tmp / "registro.jsonl"
        args_servidor = comando_servidor(caso["id"], arquivo_casos, arquivo_ferramentas, registro, catalogo_proprio)
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        if agente == "claude-code":
            cfg = tmp / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {"opsgate": {
                "command": sys.executable, "args": args_servidor, "env": {"PYTHONPATH": str(RAIZ)}}}}),
                encoding="utf-8")
            cmd = comando_claude(cfg, sistema, pedido, modelo, bare)
        else:
            cmd = comando_codex(trabalho, args_servidor, sistema, pedido, modelo)
        try:
            proc = subprocess.run(cmd, cwd=trabalho, env=env, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise AgenteIndisponivel(f"{cmd[0]} não encontrado no PATH") from e
        except subprocess.TimeoutExpired as e:
            return ler_registro(registro), f"<erro na chamada: tempo esgotado ({e.timeout:.0f}s)>", [], []
        if agente == "claude-code":
            texto, nativos = texto_claude(proc.stdout), []
        else:
            texto, nativos = eventos_codex(proc.stdout)
        if proc.returncode != 0 and not texto:
            erro = (proc.stderr or proc.stdout).strip()[-500:]
            texto = f"<erro na chamada: {cmd[0]} saiu com {proc.returncode}: {erro}>"
        return ler_registro(registro), texto, nativos, modelos_da_saida(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agente", choices=AGENTES, required=True)
    p.add_argument("--modelo-agente", help="modelo que o agente usa (--model do claude, -m do codex)")
    p.add_argument("--casos", nargs="*", help="IDs para rodar só alguns casos")
    p.add_argument("--arquivo-casos", type=Path, default=CASOS_PADRAO)
    p.add_argument("--arquivo-ferramentas", type=Path, default=FERRAMENTAS_PADRAO)
    p.add_argument("--politica", type=Path, default=RAIZ / "politica" / "politica_base.md")
    p.add_argument("--catalogo-proprio", action="store_true",
                   help="sem as ferramentas de controle da bancada e sem a instrução de formato (ver opsgate.executar)")
    p.add_argument("--saida", type=Path, default=RAIZ / "resultados")
    p.add_argument("--timeout", type=float, default=300, help="segundos por caso (padrão 300)")
    p.add_argument("--dir-temporario", type=Path,
                   help="onde criar o diretório de cada caso (padrão: o temporário do sistema; com o Codex do snap, "
                        "~/opsgate-tmp, porque o snap não enxerga o /tmp do sistema)")
    p.add_argument("--bare", action="store_true",
                   help="claude-code com --bare (sem hooks, plugins nem CLAUDE.md do usuário); só autentica com "
                        "ANTHROPIC_API_KEY, não com o login da assinatura, que responde 'Not logged in'")
    args = p.parse_args(argv)

    ferramentas = carregar_ferramentas(args.arquivo_ferramentas)
    casos = carregar_casos(args.arquivo_casos)
    problemas = validar(casos, ferramentas)
    if problemas:
        print("Conjunto de casos inválido:\n" + "\n".join(problemas), file=sys.stderr)
        return 2
    if args.casos:
        casos = [c for c in casos if c["id"] in set(args.casos)]
    politica = args.politica.read_text(encoding="utf-8")
    try:
        nome = f"{args.agente} {versao(args.agente)}" + (f" ({args.modelo_agente})" if args.modelo_agente else "")
    except AgenteIndisponivel as e:
        print(str(e), file=sys.stderr)
        return 3
    base = _diretorio_base(args.agente, args.dir_temporario)

    resultados = []
    modelos_internos: set[str] = set()
    for caso in casos:
        inicio = time.monotonic()
        try:
            chamadas, texto, nativos, modelos = rodar_caso(
                caso, args.agente, politica, ferramentas, args.arquivo_casos, args.arquivo_ferramentas, base,
                args.timeout, args.modelo_agente, args.catalogo_proprio, args.bare)
        except AgenteIndisponivel as e:
            print(f"\n{e} em {caso['id']}; rodada interrompida, nenhum relatório gravado.", file=sys.stderr)
            return 3
        modelos_internos.update(modelos)
        historico = historico_do_registro(chamadas, texto)
        resposta = resposta_do_agente(historico, ferramentas, nativos)
        classe = SAIDA_INVALIDA if nativos else classificar(caso, resposta)
        resultados.append({
            "id": caso["id"], "categoria": caso["categoria"], "esperado": caso["esperado"],
            "decisao": (resposta or {}).get("decisao"), "ferramenta": (resposta or {}).get("ferramenta"),
            "efeito": (resposta or {}).get("efeito"), "por_texto": bool((resposta or {}).get("por_texto")),
            "classe": classe, "segundos": round(time.monotonic() - inicio, 1),
            "resposta_bruta": json.dumps(historico, ensure_ascii=False),
            **({"nativo": nativos} if nativos else {}),
        })
        print(f"{caso['id']:<8} esperado={caso['esperado']:<21} obtido={str((resposta or {}).get('decisao')):<21} "
              f"{classe}{' (ação nativa fora do catálogo)' if nativos else ''}")

    if args.dir_temporario is None and base != Path(tempfile.gettempdir()):
        try:
            base.rmdir()  # ~/opsgate-tmp do Codex do snap: some quando vazio
        except OSError:
            pass
    resumo = resumir(resultados)
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.saida.mkdir(parents=True, exist_ok=True)
    sufixo = "" if args.politica.name == "politica_base.md" else f"_{args.politica.stem}"
    arquivo = args.saida / f"{carimbo}_{re.sub(r'[^A-Za-z0-9._-]+', '-', nome).strip('-')}_agente{sufixo}.json"
    externo = {} if args.arquivo_casos == CASOS_PADRAO else {"arquivo_casos": args.arquivo_casos.name}
    if args.arquivo_ferramentas != FERRAMENTAS_PADRAO:
        externo["arquivo_ferramentas"] = args.arquivo_ferramentas.name
    arquivo.write_text(json.dumps({"modelo": nome, "modo": "agente", "agente": args.agente,
                                   "modelo_interno": sorted(modelos_internos),
                                   "politica": args.politica.name, "temperatura": None, **externo,
                                   **({"catalogo_proprio": True} if args.catalogo_proprio else {}),
                                   "resumo": resumo, "resultados": resultados}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"\nAcertos: {resumo['acertos']}/{resumo['total']} ({resumo['taxa_acerto']:.0%})")
    print("Por classe: " + ", ".join(f"{c}={resumo['por_classe'][c]}" for c in CLASSES))
    print(f"Relatório: {arquivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
