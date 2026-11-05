"""Roda a bancada contra um modelo com API compatível com a da OpenAI (OpenAI, Ollama, vLLM, LiteLLM...).

Uso:
    OPSGATE_BASE_URL=http://localhost:11434/v1 OPSGATE_MODELO=llama3.2:1b python -m opsgate.executar
Variáveis: OPSGATE_BASE_URL, OPSGATE_MODELO, OPSGATE_API_KEY (opcional), OPSGATE_TEMPERATURA (padrão 0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .avaliacao import (CLASSES, MAX_TURNOS, MODOS, classificar, decisao_por_chamadas, extrair_resposta,
                        ferramentas_openai, montar_mensagens, resumir, saida_simulada)
from .dados import CASOS_PADRAO, FERRAMENTAS_PADRAO, RAIZ, carregar_casos, carregar_ferramentas, validar


class ServidorInacessivel(RuntimeError):
    """O endpoint recusou a conexão; a rodada é interrompida sem gravar relatório."""


def chamar_modelo(base_url: str, modelo: str, mensagens: list[dict], api_key: str | None, temperatura: float,
                  timeout: float = 300, sem_raciocinio: bool = False, tools: list[dict] | None = None) -> dict:
    """Devolve a mensagem do assistente (content e, no modo ferramentas, tool_calls)."""
    payload = {"model": modelo, "messages": mensagens, "temperature": temperatura}
    if tools:
        payload["tools"] = tools
    if sem_raciocinio:
        payload["think"] = False  # extensão do Ollama; separa ou desliga o raciocínio de modelos como o qwen3
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dados = json.load(resp)
    return dados["choices"][0]["message"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("OPSGATE_BASE_URL"))
    p.add_argument("--modelo", default=os.environ.get("OPSGATE_MODELO"))
    p.add_argument("--temperatura", type=float, default=float(os.environ.get("OPSGATE_TEMPERATURA", "0")))
    p.add_argument("--casos", nargs="*", help="IDs para rodar só alguns casos")
    p.add_argument("--timeout", type=float, default=300, help="segundos por chamada (padrão 300)")
    p.add_argument("--sem-raciocinio", action="store_true",
                   help="envia think=false (Ollama), para modelos com modo de raciocínio como o qwen3")
    p.add_argument("--modo", choices=MODOS, default="json",
                   help="json: o modelo declara a decisão; ferramentas: a decisão é inferida das ferramentas que ele chama")
    p.add_argument("--politica", type=Path, default=RAIZ / "politica" / "politica_base.md",
                   help="arquivo com as regras dadas ao modelo (ex.: politica/politica_minima.md, sem regras)")
    p.add_argument("--saida", type=Path, default=RAIZ / "resultados")
    p.add_argument("--arquivo-casos", type=Path, default=CASOS_PADRAO,
                   help="casos em JSONL (padrão: dados/casos.jsonl); use para casos privados fora do repositório")
    p.add_argument("--arquivo-ferramentas", type=Path, default=FERRAMENTAS_PADRAO,
                   help="catálogo de ferramentas em JSON (padrão: dados/ferramentas.json)")
    args = p.parse_args(argv)
    if not args.base_url or not args.modelo:
        p.error("informe --base-url e --modelo (ou OPSGATE_BASE_URL e OPSGATE_MODELO)")

    ferramentas = carregar_ferramentas(args.arquivo_ferramentas)
    casos = carregar_casos(args.arquivo_casos)
    problemas = validar(casos, ferramentas)
    if problemas:
        print("Conjunto de casos inválido:\n" + "\n".join(problemas), file=sys.stderr)
        return 2
    if args.casos:
        casos = [c for c in casos if c["id"] in set(args.casos)]
    politica = args.politica.read_text(encoding="utf-8")

    def chamar(mensagens: list[dict], tools: list[dict] | None) -> dict:
        try:
            return chamar_modelo(args.base_url, args.modelo, mensagens, os.environ.get("OPSGATE_API_KEY"),
                                 args.temperatura, args.timeout, args.sem_raciocinio, tools)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                # Servidor fora do ar: continuar só encheria o relatório de saídas inválidas falsas.
                raise ServidorInacessivel(str(e.reason)) from e
            return {"content": f"<erro na chamada: {type(e).__name__}: {e}>"}
        except OSError as e:
            # Tempo esgotado, erro HTTP (ex.: modelo sem suporte a ferramentas) ou rede: saída inválida do caso,
            # sem abortar a rodada.
            return {"content": f"<erro na chamada: {type(e).__name__}: {e}>"}
        except (KeyError, ValueError) as e:
            return {"content": f"<resposta malformada: {type(e).__name__}: {e}>"}

    def rodar_caso(caso: dict, args, politica: str, ferramentas: dict) -> tuple[dict | None, str]:
        mensagens = montar_mensagens(caso, politica, ferramentas, args.modo)
        if args.modo == "json":
            bruta = chamar(mensagens, None).get("content") or ""
            return extrair_resposta(bruta), bruta
        # Modo ferramentas: leituras recebem uma saída simulada e o modelo continua, até chamar algo com efeito,
        # uma ferramenta de controle, parar de chamar ferramentas ou esgotar os turnos.
        tools = ferramentas_openai(caso, ferramentas)
        chamadas: list[str] = []
        historico = []
        for _ in range(MAX_TURNOS):
            msg = chamar(mensagens, tools)
            tool_calls = msg.get("tool_calls") or []
            historico.append({"content": msg.get("content"), "tool_calls": tool_calls})
            nomes = [c["function"]["name"] for c in tool_calls]
            chamadas += nomes
            so_leitura = nomes and all(ferramentas.get(n, {}).get("tipo") == "leitura" for n in nomes)
            if not so_leitura:
                break
            mensagens = mensagens + [{"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}]
            mensagens += [{"role": "tool", "tool_call_id": c.get("id", ""), "content": saida_simulada(caso, c["function"]["name"])}
                          for c in tool_calls]
        return decisao_por_chamadas(chamadas, ferramentas), json.dumps(historico, ensure_ascii=False)

    resultados = []
    for caso in casos:
        inicio = time.monotonic()
        try:
            resposta, bruta = rodar_caso(caso, args, politica, ferramentas)
        except ServidorInacessivel as e:
            print(f"\nServidor inacessível em {caso['id']} ({e}); rodada interrompida, nenhum relatório gravado.",
                  file=sys.stderr)
            return 3
        classe = classificar(caso, resposta)
        resultados.append({
            "id": caso["id"], "categoria": caso["categoria"], "esperado": caso["esperado"],
            "decisao": (resposta or {}).get("decisao"), "ferramenta": (resposta or {}).get("ferramenta"),
            "efeito": (resposta or {}).get("efeito"),
            "classe": classe, "segundos": round(time.monotonic() - inicio, 1), "resposta_bruta": bruta,
        })
        print(f"{caso['id']:<8} esperado={caso['esperado']:<21} obtido={str((resposta or {}).get('decisao')):<21} {classe}")

    resumo = resumir(resultados)
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.saida.mkdir(parents=True, exist_ok=True)
    sufixo = "" if args.politica.name == "politica_base.md" else f"_{args.politica.stem}"
    arquivo = args.saida / f"{carimbo}_{args.modelo.replace('/', '_').replace(':', '-')}_{args.modo}{sufixo}.json"
    externo = {} if args.arquivo_casos == CASOS_PADRAO else {"arquivo_casos": args.arquivo_casos.name}
    arquivo.write_text(json.dumps({"modelo": args.modelo, "modo": args.modo, "politica": args.politica.name,
                                   "temperatura": args.temperatura, **externo,
                                   "resumo": resumo, "resultados": resultados}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"\nAcertos: {resumo['acertos']}/{resumo['total']} ({resumo['taxa_acerto']:.0%})")
    print("Por classe: " + ", ".join(f"{c}={resumo['por_classe'][c]}" for c in CLASSES))
    print(f"Relatório: {arquivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
