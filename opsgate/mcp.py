"""Servidor MCP (stdio, JSON-RPC 2.0, uma mensagem por linha) com as ferramentas de UM caso.

Uso:
    python -m opsgate.mcp --caso OPS-008 --arquivo-casos dados/casos.jsonl \\
        --arquivo-ferramentas dados/ferramentas.json --registro /caminho/registro.jsonl [--catalogo-proprio]

Serve para avaliar um agente de verdade (Claude Code, Codex) que fala MCP: `tools/list` oferece as mesmas
ferramentas do modo ferramentas (`ferramentas_openai`), e cada `tools/call` é gravada no registro, na ordem, sem
executar nada. Leituras devolvem a saída simulada do caso (os dados, onde vêm as instruções injetadas); as outras
devolvem só "registrado: <nome>". Nada além de JSON-RPC sai no stdout; avisos vão para o stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .avaliacao import ferramentas_openai, saida_simulada
from .dados import carregar_casos, carregar_ferramentas

VERSAO_PROTOCOLO = "2025-06-18"


class Servidor:
    def __init__(self, caso: dict, ferramentas: dict, registro: Path, catalogo_proprio: bool = False):
        self.caso = caso
        self.ferramentas = ferramentas
        self.registro = registro
        self.tools = [{"name": t["function"]["name"], "description": t["function"]["description"],
                       "inputSchema": t["function"]["parameters"]}
                      for t in ferramentas_openai(caso, ferramentas, catalogo_proprio)]
        self.nomes = {t["name"] for t in self.tools}

    def responder(self, msg: dict) -> dict | None:
        """Resposta JSON-RPC à mensagem, ou None para notificações."""
        metodo, ident, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if "id" not in msg:
            return None  # notificação (notifications/initialized e afins): sem resposta
        if metodo == "initialize":
            resultado = {"protocolVersion": params.get("protocolVersion") or VERSAO_PROTOCOLO,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "opsgate", "version": _versao()}}
        elif metodo == "ping":
            resultado = {}
        elif metodo == "tools/list":
            resultado = {"tools": self.tools}
        elif metodo == "tools/call":
            nome = params.get("name")
            if nome not in self.nomes:
                return _erro(ident, -32602, f"ferramenta desconhecida: {nome}")
            self._registrar(nome, params.get("arguments") or {})
            texto = (saida_simulada(self.caso, nome) if self.ferramentas.get(nome, {}).get("tipo") == "leitura"
                     else f"registrado: {nome}")
            resultado = {"content": [{"type": "text", "text": texto}]}
        else:
            return _erro(ident, -32601, f"método não suportado: {metodo}")
        return {"jsonrpc": "2.0", "id": ident, "result": resultado}

    def _registrar(self, nome: str, argumentos) -> None:
        with self.registro.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"name": nome, "arguments": argumentos}, ensure_ascii=False) + "\n")
            f.flush()


def _erro(ident, codigo: int, mensagem: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": codigo, "message": mensagem}}


def _versao() -> str:
    try:
        from importlib.metadata import version
        return version("opsgate")
    except Exception:  # pacote não instalado (rodando do repositório)
        return "dev"


def servir(servidor: Servidor, entrada=None, saida=None) -> None:
    entrada = entrada or sys.stdin
    saida = saida or sys.stdout
    for linha in entrada:
        if not linha.strip():
            continue
        try:
            msg = json.loads(linha)
        except json.JSONDecodeError as e:
            resposta = _erro(None, -32700, f"JSON inválido: {e}")
        else:
            if not isinstance(msg, dict):
                resposta = _erro(None, -32600, "requisição inválida")
            else:
                resposta = servidor.responder(msg)
        if resposta is not None:
            saida.write(json.dumps(resposta, ensure_ascii=False) + "\n")
            saida.flush()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--caso", required=True)
    p.add_argument("--arquivo-casos", type=Path, required=True)
    p.add_argument("--arquivo-ferramentas", type=Path, required=True)
    p.add_argument("--registro", type=Path, required=True, help="JSONL onde cada chamada é gravada, na ordem")
    p.add_argument("--catalogo-proprio", action="store_true",
                   help="sem as ferramentas de controle da bancada (ver opsgate.executar)")
    args = p.parse_args(argv)
    casos = {c["id"]: c for c in carregar_casos(args.arquivo_casos)}
    if args.caso not in casos:
        print(f"caso {args.caso} não está em {args.arquivo_casos}", file=sys.stderr)
        return 2
    servir(Servidor(casos[args.caso], carregar_ferramentas(args.arquivo_ferramentas), args.registro,
                    args.catalogo_proprio))
    return 0


if __name__ == "__main__":
    sys.exit(main())
