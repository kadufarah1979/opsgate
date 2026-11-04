"""Roda a bancada contra um modelo com API compatível com a da OpenAI (OpenAI, Ollama, vLLM, LiteLLM...).

Uso:
    SEGOPS_BASE_URL=http://localhost:11434/v1 SEGOPS_MODELO=llama3.2:1b python -m segops.executar
Variáveis: SEGOPS_BASE_URL, SEGOPS_MODELO, SEGOPS_API_KEY (opcional), SEGOPS_TEMPERATURA (padrão 0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .avaliacao import CLASSES, classificar, extrair_resposta, montar_mensagens, resumir
from .dados import RAIZ, carregar_casos, carregar_ferramentas, validar


def chamar_modelo(base_url: str, modelo: str, mensagens: list[dict], api_key: str | None, temperatura: float,
                  timeout: float = 300, sem_raciocinio: bool = False) -> str:
    payload = {"model": modelo, "messages": mensagens, "temperature": temperatura}
    if sem_raciocinio:
        payload["think"] = False  # extensão do Ollama; separa ou desliga o raciocínio de modelos como o qwen3
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dados = json.load(resp)
    return dados["choices"][0]["message"]["content"] or ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("SEGOPS_BASE_URL"))
    p.add_argument("--modelo", default=os.environ.get("SEGOPS_MODELO"))
    p.add_argument("--temperatura", type=float, default=float(os.environ.get("SEGOPS_TEMPERATURA", "0")))
    p.add_argument("--casos", nargs="*", help="IDs para rodar só alguns casos")
    p.add_argument("--timeout", type=float, default=300, help="segundos por chamada (padrão 300)")
    p.add_argument("--sem-raciocinio", action="store_true",
                   help="envia think=false (Ollama), para modelos com modo de raciocínio como o qwen3")
    p.add_argument("--saida", type=Path, default=RAIZ / "resultados")
    args = p.parse_args(argv)
    if not args.base_url or not args.modelo:
        p.error("informe --base-url e --modelo (ou SEGOPS_BASE_URL e SEGOPS_MODELO)")

    ferramentas = carregar_ferramentas()
    casos = carregar_casos()
    problemas = validar(casos, ferramentas)
    if problemas:
        print("Conjunto de casos inválido:\n" + "\n".join(problemas), file=sys.stderr)
        return 2
    if args.casos:
        casos = [c for c in casos if c["id"] in set(args.casos)]
    politica = (RAIZ / "politica" / "politica_base.md").read_text(encoding="utf-8")

    resultados = []
    for caso in casos:
        inicio = time.monotonic()
        try:
            texto = chamar_modelo(args.base_url, args.modelo, montar_mensagens(caso, politica, ferramentas),
                                  os.environ.get("SEGOPS_API_KEY"), args.temperatura, args.timeout, args.sem_raciocinio)
        except (OSError, KeyError, ValueError) as e:
            # Tempo esgotado ou resposta malformada vira saída inválida do caso, sem abortar a rodada.
            texto = f"<erro na chamada: {type(e).__name__}: {e}>"
        resposta = extrair_resposta(texto)
        classe = classificar(caso, resposta)
        resultados.append({
            "id": caso["id"], "categoria": caso["categoria"], "esperado": caso["esperado"],
            "decisao": (resposta or {}).get("decisao"), "ferramenta": (resposta or {}).get("ferramenta"),
            "classe": classe, "segundos": round(time.monotonic() - inicio, 1), "resposta_bruta": texto,
        })
        print(f"{caso['id']:<8} esperado={caso['esperado']:<21} obtido={str((resposta or {}).get('decisao')):<21} {classe}")

    resumo = resumir(resultados)
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.saida.mkdir(parents=True, exist_ok=True)
    arquivo = args.saida / f"{carimbo}_{args.modelo.replace('/', '_').replace(':', '-')}.json"
    arquivo.write_text(json.dumps({"modelo": args.modelo, "temperatura": args.temperatura, "resumo": resumo,
                                   "resultados": resultados}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nAcertos: {resumo['acertos']}/{resumo['total']} ({resumo['taxa_acerto']:.0%})")
    print("Por classe: " + ", ".join(f"{c}={resumo['por_classe'][c]}" for c in CLASSES))
    print(f"Relatório: {arquivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
