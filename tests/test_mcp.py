"""Testes locais do mcp_server e pwa_gate com servicos simulados."""
import base64
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(ROOT))

TMP = Path("/tmp/mcp_test_secrets")
TMP.mkdir(exist_ok=True)
os.environ["SECRETS_DIR"] = str(TMP)
os.environ["FATURAS_DIR"] = str(TMP / "faturas")
(TMP / "mcp_token.txt").write_text("tok123\n")
(TMP / "faturas").mkdir(exist_ok=True)


# ---- stubs dos services ----------------------------------------------
class _Hdr:
    pass


def _fake_msg(mid, tid, subj, body_plain=None, body_html=None, att=False):
    parts = []
    if body_plain is not None:
        parts.append({"mimeType": "text/plain",
                      "body": {"data": base64.urlsafe_b64encode(body_plain.encode()).decode()}})
    if body_html is not None:
        parts.append({"mimeType": "text/html",
                      "body": {"data": base64.urlsafe_b64encode(body_html.encode()).decode()}})
    if att:
        parts.append({"mimeType": "application/pdf", "filename": "extrato.pdf",
                      "body": {"attachmentId": "att1", "size": 10}})
    return {"id": mid, "threadId": tid, "snippet": "snip", "labelIds": ["INBOX"],
            "internalDate": "1757340000000",
            "payload": {"mimeType": "multipart/mixed", "headers": [
                {"name": "From", "value": "Eugénia <eugenia@eugest.pt>"},
                {"name": "To", "value": "david.sardinha@omnai.pt"},
                {"name": "Subject", "value": subj},
                {"name": "Date", "value": "Mon, 8 Sep 2026 10:00:00 +0100"},
                {"name": "Message-ID", "value": "<abc@eugest.pt>"},
            ], "parts": parts}}


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class _Svc:
    def users(self):
        return self

    def messages(self):
        return self

    def threads(self):
        return self

    def attachments(self):
        return self

    def drafts(self):
        return self

    def list(self, userId, q, maxResults):
        return _Exec({"messages": [{"id": "m1"}], "resultSizeEstimate": 1})

    def get(self, userId, id=None, format=None, metadataHeaders=None, messageId=None):
        if messageId:  # attachment
            return _Exec({"data": base64.urlsafe_b64encode(b"%PDF-1.4 fake").decode()})
        if id == "t1":
            return _Exec({"messages": [
                _fake_msg("m1", "t1", "Faturas em falta", None, "<html><body><p>Bom dia <b>David</b></p><table><tr><td>FT 1/23</td></tr></table></body></html>", att=True)]})
        return _Exec(_fake_msg("m1", "t1", "Faturas em falta", "ola"))

    def getProfile(self, userId):
        return _Exec({"emailAddress": "x", "messagesTotal": 1})

    def create(self, userId, body):
        return _Exec({"id": "d1", "message": {"id": "dm1"}})


gmail = types.ModuleType("services.gmail")
gmail.GMAIL_ACCOUNTS = {"david.sardinha@omnai.pt": "a", "davidsardinhalves@gmail.com": "b"}
gmail._service = lambda acc: _Svc()


def _header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _walk(part, out):
    if part.get("filename") and (part.get("body") or {}).get("attachmentId"):
        out.append({"filename": part["filename"], "mime_type": part["mimeType"],
                    "attachment_id": part["body"]["attachmentId"], "size": 10})
    for s in part.get("parts", []) or []:
        _walk(s, out)


def list_attachments(msg):
    out = []
    _walk(msg.get("payload", {}), out)
    return out


gmail._header = _header
gmail.list_attachments = list_attachments
gmail.download_attachment = lambda a, m, i: b"%PDF-1.4 fake"
gmail.test_connection = lambda a: {"ok": True, "email": a}
gmail.create_draft = lambda *a, **k: {"id": "d1", "message": {"id": "dm1"}}

services = types.ModuleType("services")
services.gmail = gmail
sys.modules["services"] = services
sys.modules["services.gmail"] = gmail

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import mcp_server  # noqa: E402
import pwa_gate  # noqa: E402

app = FastAPI()
app.include_router(mcp_server.router)


@app.get("/api/x")
async def api_x():
    return {"ok": True}


@app.get("/hoje")
async def hoje():
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<h1>hoje</h1>")


@app.get("/health")
async def health():
    return {"ok": True}


pwa_gate.install(app)
client = TestClient(app, base_url="https://testserver")
H = {"Authorization": "Bearer tok123"}


def rpc(method, params=None, id_=1, headers=H, path="/mcp"):
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers)


def test_auth_required():
    assert rpc("ping", headers={}).status_code == 401
    assert rpc("ping", headers={"X-OMNAI-Token": "tok123"}).status_code == 200
    assert rpc("ping", headers={}, path="/mcp/tok123").status_code == 200
    assert rpc("ping", headers={}, path="/mcp/errado").status_code == 401


def test_initialize_and_list():
    r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}})
    assert r.status_code == 200
    j = r.json()
    assert j["result"]["protocolVersion"] == "2025-06-18"
    assert "Mcp-Session-Id" in r.headers
    r = rpc("notifications/initialized", id_=None)
    assert r.status_code == 202
    r = rpc("tools/list")
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "search_mail" in names and "create_draft" in names
    assert r.json()["result"]["tools"][0]["inputSchema"]["type"] == "object"


def test_search_thread_attachment():
    r = rpc("tools/call", {"name": "search_mail", "arguments": {"query": "from:eugest"}})
    j = r.json()["result"]
    assert j["isError"] is False
    data = json.loads(j["content"][0]["text"])
    assert data["results"][0]["messages"][0]["subject"] == "Faturas em falta"
    assert len(data["results"]) == 2  # all accounts

    r = rpc("tools/call", {"name": "get_thread", "arguments": {
        "account": "david.sardinha@omnai.pt", "thread_id": "t1"}})
    data = json.loads(r.json()["result"]["content"][0]["text"])
    m = data["messages"][0]
    assert "Bom dia David" in m["body"] and "FT 1/23" in m["body"]
    assert m["attachments"][0]["attachment_id"] == "att1"

    r = rpc("tools/call", {"name": "read_attachment", "arguments": {
        "account": "david.sardinha@omnai.pt", "message_id": "m1",
        "attachment_id": "att1", "filename": "extrato.pdf"}})
    data = json.loads(r.json()["result"]["content"][0]["text"])
    assert "text" in data  # pdf falso -> mensagem de ilegivel, mas caminho coberto


def test_bad_args_and_unknown():
    r = rpc("tools/call", {"name": "get_thread", "arguments": {"account": "x@y", "thread_id": "t"}})
    assert r.json()["result"]["isError"] is True
    r = rpc("tools/call", {"name": "nao_existe", "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    r = rpc("metodo/estranho")
    assert r.json()["error"]["code"] == -32601


def test_batch():
    r = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                  {"jsonrpc": "2.0", "method": "notifications/x"}], headers=H)
    assert r.status_code == 200 and len(r.json()) == 1


def test_gate_off_by_default():
    assert client.get("/api/x").status_code == 200


def test_gate_on():
    (TMP / "pwa_gate.json").write_text(json.dumps({"frase": "abre-te", "chave": "k"}))
    try:
        assert client.get("/health").status_code == 200
        assert rpc("ping").status_code == 200
        assert client.get("/api/x").status_code == 401
        r = client.get("/hoje", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/entrar")
        r = client.post("/entrar", data={"frase": "errada", "next": "/hoje"})
        assert r.status_code == 401
        r = client.post("/entrar", data={"frase": "abre-te", "next": "/hoje"},
                        follow_redirects=False)
        assert r.status_code == 303 and "omnai_sessao" in r.headers.get("set-cookie", "")
        assert client.get("/api/x").status_code == 200
        assert client.get("/hoje").status_code == 200
    finally:
        (TMP / "pwa_gate.json").unlink()
