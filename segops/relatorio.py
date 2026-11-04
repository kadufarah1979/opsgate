"""Junta relatórios de rodadas em uma tabela Markdown comparando modelos.

Uso: python -m segops.relatorio relatorios/v0.2/*.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def linha(dados: dict) -> dict:
    res = dados["resultados"]
    injecao = [r for r in res if r["categoria"] == "injecao"]
    seg = [r["segundos"] for r in res]
    pc = dados["resumo"]["por_classe"]
    return {
        "modelo": dados["modelo"],
        "acertos": dados["resumo"]["acertos"],
        "total": dados["resumo"]["total"],
        "falha_critica": pc["falha_critica"],
        "excesso_cautela": pc["excesso_cautela"],
        "outros": pc["erro"] + pc["ferramenta_errada"] + pc["saida_invalida"],
        "injecao_ok": sum(r["classe"] == "correto" for r in injecao),
        "injecao_total": len(injecao),
        "seg_medio": sum(seg) / len(seg) if seg else 0.0,
    }


def tabela(arquivos: list[Path]) -> str:
    linhas = sorted((linha(json.loads(a.read_text(encoding="utf-8"))) for a in arquivos),
                    key=lambda l: (l["falha_critica"], -l["acertos"]))
    out = ["| Modelo | Acertos | Falhas críticas | Excesso de cautela | Outros erros | Injeção correta | s/caso |",
           "|---|---|---|---|---|---|---|"]
    for l in linhas:
        out.append(f"| {l['modelo']} | {l['acertos']}/{l['total']} ({l['acertos'] / l['total']:.0%}) | "
                   f"{l['falha_critica']} | {l['excesso_cautela']} | {l['outros']} | "
                   f"{l['injecao_ok']}/{l['injecao_total']} | {l['seg_medio']:.1f} |")
    return "\n".join(out)


if __name__ == "__main__":
    print(tabela([Path(a) for a in sys.argv[1:]]))
