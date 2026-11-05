"""Junta relatórios de rodadas em uma tabela Markdown comparando modelos.

Uso: python -m opsgate.relatorio [--casos-atuais] [--arquivo-casos casos.jsonl] relatorios/v0.2/*.json

--casos-atuais reclassifica as decisões gravadas com os rótulos atuais de dados/casos.jsonl,
para comparar rodadas antigas depois de correções no conjunto sem chamar o modelo de novo. Rodadas feitas com
casos de fora do repositório (--arquivo-casos no executor) pedem o mesmo arquivo aqui.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .avaliacao import classificar, resumir
from .dados import carregar_casos


def reclassificar(dados: dict, casos: list[dict] | None = None) -> dict:
    por_id = {c["id"]: c for c in (casos if casos is not None else carregar_casos())}
    faltando = sorted({r["id"] for r in dados["resultados"]} - por_id.keys())
    if faltando:
        origem = dados.get("arquivo_casos", "dados/casos.jsonl")
        raise ValueError(f"rodada de {dados['modelo']} usou casos de {origem}; ids ausentes no conjunto informado: "
                         f"{', '.join(faltando[:5])}{'...' if len(faltando) > 5 else ''}")
    casos = por_id
    resultados = []
    for r in dados["resultados"]:
        resposta = ({"decisao": r["decisao"], "ferramenta": r["ferramenta"], "efeito": r.get("efeito")}
                    if r["decisao"] else None)
        resultados.append({**r, "classe": classificar(casos[r["id"]], resposta)})
    return {**dados, "resultados": resultados, "resumo": resumir(resultados)}


def linha(dados: dict) -> dict:
    res = dados["resultados"]
    injecao = [r for r in res if r["categoria"] == "injecao"]
    seg = [r["segundos"] for r in res]
    pc = dados["resumo"]["por_classe"]
    return {
        "modelo": dados["modelo"] + (" · ferramentas" if dados.get("modo") == "ferramentas" else "")
                  + (f" · {dados['politica'].removesuffix('.md')}" if dados.get("politica", "politica_base.md")
                     != "politica_base.md" else ""),
        "acertos": dados["resumo"]["acertos"],
        "total": dados["resumo"]["total"],
        "falha_critica": pc["falha_critica"],
        "excesso_cautela": pc["excesso_cautela"],
        "outros": pc["erro"] + pc["ferramenta_errada"] + pc["saida_invalida"],
        "injecao_ok": sum(r["classe"] == "correto" for r in injecao),
        "injecao_total": len(injecao),
        "seg_medio": sum(seg) / len(seg) if seg else 0.0,
    }


def tabela(arquivos: list[Path], casos_atuais: bool = False, arquivo_casos: Path | None = None) -> str:
    rodadas = [json.loads(a.read_text(encoding="utf-8")) for a in arquivos]
    if casos_atuais:
        casos = carregar_casos(arquivo_casos) if arquivo_casos else None
        rodadas = [reclassificar(d, casos) for d in rodadas]
    linhas = sorted((linha(d) for d in rodadas), key=lambda l: (l["falha_critica"], -l["acertos"]))
    out = ["| Modelo | Acertos | Falhas críticas | Excesso de cautela | Outros erros | Injeção correta | s/caso |",
           "|---|---|---|---|---|---|---|"]
    for l in linhas:
        out.append(f"| {l['modelo']} | {l['acertos']}/{l['total']} ({l['acertos'] / l['total']:.0%}) | "
                   f"{l['falha_critica']} | {l['excesso_cautela']} | {l['outros']} | "
                   f"{l['injecao_ok']}/{l['injecao_total']} | {l['seg_medio']:.1f} |")
    return "\n".join(out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--casos-atuais", action="store_true")
    p.add_argument("--arquivo-casos", type=Path, help="casos em JSONL usados nas rodadas (padrão: dados/casos.jsonl)")
    p.add_argument("arquivos", nargs="+", type=Path)
    a = p.parse_args()
    try:
        print(tabela(a.arquivos, a.casos_atuais, a.arquivo_casos))
    except ValueError as e:
        p.error(str(e))
