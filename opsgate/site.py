"""Gera a página de resultados (docs/index.html) para o GitHub Pages a partir de relatorios/.

Uso: python -m opsgate.site
Reclassifica cada rodada com os rótulos atuais, para todas as linhas usarem o mesmo gabarito.
"""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

from .dados import RAIZ, carregar_casos
from .relatorio import linha, reclassificar

REPO = "https://github.com/kadufarah1979/opsgate"
PAGINA = "https://kadufarah1979.github.io/opsgate/"
DESCRICAO = ("Bancada aberta em português: agentes de IA de operação sabem quando executar, pedir aprovação, "
             "recusar ou perguntar?")


def gerar(relatorios: list[Path], destino: Path) -> int:
    # Uma linha por (modelo, modo, política): vale a rodada mais recente (o nome do arquivo começa
    # com o carimbo de tempo UTC), para versões antigas não duplicarem o mesmo modelo.
    recentes: dict[tuple, tuple[str, dict]] = {}
    for a in relatorios:
        dados = json.loads(a.read_text(encoding="utf-8"))
        chave = (dados["modelo"], dados.get("modo", "json"), dados.get("politica", "politica_base.md"))
        if chave not in recentes or a.name > recentes[chave][0]:
            recentes[chave] = (a.name, dados)
    linhas = sorted((linha(reclassificar(d)) for _, d in recentes.values()),
                    key=lambda l: (l["falha_critica"], -l["acertos"]))
    casos = carregar_casos()
    contagem: dict[str, int] = {}
    for c in casos:
        contagem[c["esperado"]] = contagem.get(c["esperado"], 0) + 1
    injecao = sum(c["categoria"] == "injecao" for c in casos)

    def celula(v) -> str:
        return f"<td>{html.escape(str(v))}</td>"

    corpo = "\n".join(
        "<tr>" + celula(l["modelo"]) + celula(f"{l['acertos']}/{l['total']}")
        + f'<td class="crit">{l["falha_critica"]}</td>' + celula(l["excesso_cautela"]) + celula(l["outros"])
        + celula(f"{l['injecao_ok']}/{l['injecao_total']}") + "</tr>"
        for l in linhas
    )
    pagina = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpsGate — segurança de agentes de IA em operações</title>
<meta name="description" content="{DESCRICAO}">
<meta property="og:type" content="website">
<meta property="og:url" content="{PAGINA}">
<meta property="og:title" content="OpsGate — seu agente de IA sabe quando não executar?">
<meta property="og:description" content="{DESCRICAO}">
<meta property="og:image" content="{PAGINA}opsgate.png">
<meta property="og:image:width" content="1280">
<meta property="og:image:height" content="640">
<meta property="og:image:alt" content="OpsGate: um pedido chega ao agente, que decide entre executar, pedir aprovação, recusar ou perguntar">
<meta name="twitter:card" content="summary_large_image">
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", Arial, sans-serif; max-width: 920px; margin: 0 auto; padding: 32px 20px 64px; color: #24302f; line-height: 1.6; }}
  h1 {{ color: #173D3B; margin-bottom: 4px; }}
  .sub {{ color: #61716E; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: .95rem; }}
  th, td {{ border-bottom: 1px solid #dde3de; padding: 8px 10px; text-align: left; }}
  th {{ background: #173D3B; color: #fff; }}
  td.crit {{ font-weight: 700; color: #b03a1e; }}
  code {{ background: #eef2ee; padding: 1px 5px; border-radius: 4px; }}
  a {{ color: #C56A39; }}
  footer {{ color: #61716E; font-size: .9rem; margin-top: 40px; }}
</style>
</head>
<body>
<h1>OpsGate</h1>
<p class="sub">Agentes de IA de operação sabem quando <strong>não</strong> executar?</p>
<p>{len(casos)} pedidos reais de operação (Zabbix, Wazuh, AWS, Kubernetes, Terraform), cada um com a decisão correta segundo uma política de 8 regras: executar ({contagem.get('executar', 0)}), pedir aprovação ({contagem.get('pedir_aprovacao', 0)}), recusar ({contagem.get('recusar', 0)}) ou pedir esclarecimento ({contagem.get('pedir_esclarecimento', 0)}). {injecao} casos trazem instruções injetadas em alertas, logs, chamados e descrições de PR.</p>
<p>No modo <code>json</code> o modelo declara o que faria; no modo <code>ferramentas</code> ele recebe as ferramentas de verdade e a decisão é o que ele chama. <strong>Falha crítica</strong> é executar o que devia passar por aprovação ou ser recusado.</p>
<table>
<tr><th>Modelo</th><th>Acertos</th><th>Falhas críticas</th><th>Excesso de cautela</th><th>Outros erros</th><th>Injeção correta</th></tr>
{corpo}
</table>
<p>Modelos abertos via Ollama em CPU, temperatura 0. Ordenado por falhas críticas e depois por acertos. Todas as linhas usam o gabarito atual do conjunto. Respostas completas de cada caso em <a href="{REPO}/tree/main/relatorios">relatorios/</a>.</p>
<p><em>In English:</em> a Portuguese-language benchmark of {len(casos)} infrastructure-operations requests checking whether AI agents execute, ask for approval, refuse or ask for clarification. Critical failure = executing what needed approval or refusal.</p>
<p><a href="{REPO}">Código, casos e como rodar com o seu modelo</a></p>
<footer>Gerado em {date.today().isoformat()} por <code>python -m opsgate.site</code>. Código MIT; casos e política CC BY 4.0.</footer>
</body>
</html>
"""
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(pagina, encoding="utf-8")
    return len(linhas)


if __name__ == "__main__":
    n = gerar(sorted((RAIZ / "relatorios").glob("*/*.json")), RAIZ / "docs" / "index.html")
    print(f"docs/index.html com {n} rodadas")
