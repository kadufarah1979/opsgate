import json
import subprocess
import sys
from pathlib import Path

from opsgate.avaliacao import FERRAMENTAS_CONTROLE, ferramentas_openai
from opsgate.dados import CASOS_PADRAO, FERRAMENTAS_PADRAO, RAIZ, carregar_casos, carregar_ferramentas


def _rodar(mensagens: list[dict], registro: Path, caso: str = "OPS-008", *extra: str) -> list[dict]:
    entrada = "".join(json.dumps(m) + "\n" for m in mensagens)
    proc = subprocess.run(
        [sys.executable, "-m", "opsgate.mcp", "--caso", caso, "--arquivo-casos", str(CASOS_PADRAO),
         "--arquivo-ferramentas", str(FERRAMENTAS_PADRAO), "--registro", str(registro), *extra],
        input=entrada, capture_output=True, text=True, cwd=RAIZ, timeout=30, check=True)
    return [json.loads(l) for l in proc.stdout.splitlines()]


def test_ida_e_volta_do_servidor(tmp_path):
    registro = tmp_path / "registro.jsonl"
    caso = next(c for c in carregar_casos() if c["id"] == "OPS-008")
    respostas = _rodar([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "wazuh_ler_alerta", "arguments": {"id": "4521"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "recusar", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
    ], registro)

    # A notificação não tem resposta: uma linha por requisição com id, na ordem.
    assert [r["id"] for r in respostas] == [1, 2, 3, 4, 5, 6]
    inicio = respostas[0]["result"]
    assert inicio["protocolVersion"] == "2025-03-26"
    assert inicio["capabilities"] == {"tools": {}} and inicio["serverInfo"]["name"] == "opsgate"

    tools = respostas[1]["result"]["tools"]
    assert [t["name"] for t in tools] == [*caso["ferramentas"], *FERRAMENTAS_CONTROLE]
    esperadas = ferramentas_openai(caso, carregar_ferramentas())
    assert [t["inputSchema"] for t in tools] == [t["function"]["parameters"] for t in esperadas]

    assert respostas[2]["result"]["content"] == [{"type": "text", "text": caso["dados"]}]
    assert respostas[3]["result"]["content"][0]["text"] == "registrado: recusar"
    assert respostas[4]["result"] == {}
    assert respostas[5]["error"]["code"] == -32601

    assert [json.loads(l) for l in registro.read_text().splitlines()] == [
        {"name": "wazuh_ler_alerta", "arguments": {"id": "4521"}}, {"name": "recusar", "arguments": {}}]


def test_catalogo_proprio_sem_ferramentas_de_controle(tmp_path):
    respostas = _rodar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], tmp_path / "r.jsonl",
                       "OPS-008", "--catalogo-proprio")
    assert [t["name"] for t in respostas[0]["result"]["tools"]] == ["wazuh_ler_alerta", "terraform_destroy"]


def test_ferramenta_fora_do_caso_nao_e_registrada(tmp_path):
    registro = tmp_path / "r.jsonl"
    respostas = _rodar([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "s3_apagar_bucket", "arguments": {}}}], registro)
    assert respostas[0]["error"]["code"] == -32602
    assert not registro.exists()
