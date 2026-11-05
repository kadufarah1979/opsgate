"""Carga e validação do conjunto de casos."""

from __future__ import annotations

import json
from pathlib import Path

from . import DECISOES

RAIZ = Path(__file__).resolve().parent.parent
CASOS_PADRAO = RAIZ / "dados" / "casos.jsonl"
FERRAMENTAS_PADRAO = RAIZ / "dados" / "ferramentas.json"
CAMPOS_OBRIGATORIOS = (
    "id", "categoria", "ambiente", "solicitante", "ferramentas", "pedido",
    "esperado", "tambem_aceito", "ferramentas_esperadas", "ferramentas_proibidas", "justificativa",
)


def carregar_ferramentas(caminho: Path = FERRAMENTAS_PADRAO) -> dict:
    return json.loads(caminho.read_text(encoding="utf-8"))


def carregar_casos(caminho: Path = CASOS_PADRAO) -> list[dict]:
    casos = []
    for n, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), 1):
        if not linha.strip():
            continue
        try:
            casos.append(json.loads(linha))
        except json.JSONDecodeError as e:
            raise ValueError(f"{caminho.name}:{n}: JSON inválido: {e}") from e
    return casos


def validar(casos: list[dict], ferramentas: dict) -> list[str]:
    """Devolve a lista de problemas encontrados; vazia quando o conjunto está consistente."""
    problemas: list[str] = []
    vistos: set[str] = set()
    for caso in casos:
        cid = caso.get("id", "<sem id>")
        faltando = [c for c in CAMPOS_OBRIGATORIOS if c not in caso]
        if faltando:
            problemas.append(f"{cid}: campos ausentes {faltando}")
            continue
        if cid in vistos:
            problemas.append(f"{cid}: id repetido")
        vistos.add(cid)
        aceitas = [caso["esperado"], *caso["tambem_aceito"]]
        for d in aceitas:
            if d not in DECISOES:
                problemas.append(f"{cid}: decisão desconhecida {d!r}")
        if caso["esperado"] in caso["tambem_aceito"]:
            problemas.append(f"{cid}: 'esperado' repetido em 'tambem_aceito'")
        for campo in ("ferramentas", "ferramentas_esperadas", "ferramentas_proibidas"):
            for f in caso[campo]:
                if f not in ferramentas:
                    problemas.append(f"{cid}: ferramenta {f!r} em {campo} não está no catálogo")
        for f in (*caso["ferramentas_esperadas"], *caso["ferramentas_proibidas"]):
            if f not in caso["ferramentas"]:
                problemas.append(f"{cid}: {f!r} não está entre as ferramentas oferecidas no caso")
        if caso["ferramentas_esperadas"] and "executar" not in aceitas:
            problemas.append(f"{cid}: 'ferramentas_esperadas' só faz sentido quando executar é aceito")
        if set(caso["ferramentas_esperadas"]) & set(caso["ferramentas_proibidas"]):
            problemas.append(f"{cid}: ferramenta ao mesmo tempo esperada e proibida")
        sol = caso["solicitante"]
        if not isinstance(sol, dict) or not {"nome", "papel", "permissoes"} <= sol.keys():
            problemas.append(f"{cid}: solicitante precisa de nome, papel e permissoes")
    return problemas


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Valida um conjunto de casos contra o catálogo de ferramentas.")
    p.add_argument("--arquivo-casos", type=Path, default=CASOS_PADRAO)
    p.add_argument("--arquivo-ferramentas", type=Path, default=FERRAMENTAS_PADRAO)
    a = p.parse_args()
    casos = carregar_casos(a.arquivo_casos)
    problemas = validar(casos, carregar_ferramentas(a.arquivo_ferramentas))
    for problema in problemas:
        print(problema)
    contagem: dict[str, int] = {}
    for c in casos:
        contagem[c["esperado"]] = contagem.get(c["esperado"], 0) + 1
    print(f"{len(casos)} casos; decisões esperadas: {contagem}")
    sys.exit(1 if problemas else 0)
