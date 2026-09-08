"""Servidor MCP (Streamable HTTP) da stack OMNAI. 08-09-2026.

Expoe ao Claude (claude.ai, Cowork, Claude Code) as caixas Gmail de todas as
contas do David, o indice de faturas, a fila Hoje, as tarefas e os dados das
empresas, reutilizando os servicos que os workers ja usam. Sem OAuth novo:
os tokens Gmail vivem em /secrets/tokens e ja estao em producao.

Implementacao directa do JSON-RPC do protocolo MCP sobre FastAPI, sem o SDK
`mcp` (nao esta na imagem e o codigo e bind-mounted: instalar dependencias
obrigaria a rebuild). Cobre o que os clientes precisam: initialize, ping,
tools/list, tools/call, notificacoes. Respostas em JSON simples (o transporte
Streamable HTTP permite-o); GET /mcp devolve 405 porque nao ha stream
servidor -> cliente.

Autenticacao: token dedicado em /secrets/mcp_token.txt (ou env
OMNAI_MCP_TOKEN), aceite por `Authorization: Bearer`, `X-OMNAI-Token` ou como
segmento de caminho `/mcp/<token>` para clientes que nao enviam headers.
Token proprio, separado do OMNAI_API_TOKEN, para poder rodar sem tocar nos
workers.

Escritas: so `create_draft` (rascunho Gmail, nunca envia), `criar_tarefa` e
`fatura_accao`. Tudo o resto e leitura.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import io
import json
import os
import re
import secrets
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

log = structlog.get_logger()

router = APIRouter()

SERVER_INFO = {"name": "omnai-stack", "version": "1.0.0"}
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
TOKEN_FILE = SECRETS_DIR / "mcp_token.txt"
FATURAS_DIR = Path(os.getenv("FATURAS_DIR", "/faturas"))
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------- auth

def _load_token() -> str:
    tok = os.getenv("OMNAI_MCP_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _presented_token(request: Request, path_token: str | None) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    hdr = request.headers.get("x-omnai-token", "")
    if hdr:
        return hdr.strip()
    return (path_token or "").strip()


def _authorized(request: Request, path_token: str | None) -> bool:
    expected = _load_token()
    given = _presented_token(request, path_token)
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


def _unauthorized() -> Response:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None,
         "error": {"code": -32001, "message": "nao autorizado"}},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="omnai-mcp"'},
    )


# ------------------------------------------------------------ helpers

def _json_default(v: Any):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, bytes):
        return base64.b64encode(v).decode("ascii")
    return str(v)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default, indent=1)


def _text_result(obj: Any, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": _dumps(obj)}], "isError": is_error}


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "head"]):
            t.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for blk in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4",
                                  "h5", "h6", "table", "blockquote"]):
            blk.append("\n")
        for td in soup.find_all(["td", "th"]):
            td.append(" | ")
        text = soup.get_text(" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _decode_part(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_full(msg: dict, max_chars: int) -> str:
    """text/plain primeiro; se nao houver, text/html convertido. Cobre partes
    aninhadas (multipart/alternative dentro de multipart/mixed)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mt = part.get("mimeType", "")
        if mt == "text/plain":
            plain.append(_decode_part(part))
        elif mt == "text/html":
            html.append(_decode_part(part))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}) or {})
    body = "\n".join(p for p in plain if p.strip())
    if not body.strip() and html:
        body = _html_to_text("\n".join(html))
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n[... truncado, {len(body)} caracteres no total]"
    return body


def _pdf_text(data: bytes, max_chars: int = 40000) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, p in enumerate(reader.pages):
            try:
                pages.append(f"--- pagina {i + 1} ---\n{p.extract_text() or ''}")
            except Exception as exc:  # pagina corrompida nao mata o resto
                pages.append(f"--- pagina {i + 1} --- [erro: {exc}]")
        txt = "\n".join(pages).strip()
    except Exception as exc:
        return f"[pdf ilegivel: {type(exc).__name__}: {exc}]"
    if len(txt) > max_chars:
        txt = txt[:max_chars] + f"\n[... truncado, {len(txt)} caracteres no total]"
    return txt or "[pdf sem camada de texto; provavelmente digitalizado]"


# -------------------------------------------------------- gmail layer

def _gmail():
    from services import gmail
    return gmail


def _accounts() -> list[str]:
    return list(_gmail().GMAIL_ACCOUNTS.keys())


def _resolve_accounts(account: str | None) -> list[str]:
    if not account or account.lower() in ("all", "todas", "*"):
        return _accounts()
    if account not in _gmail().GMAIL_ACCOUNTS:
        raise ValueError(
            f"conta desconhecida: {account}. Conhecidas: {', '.join(_accounts())}")
    return [account]


def _msg_meta(gm, account: str, msg: dict) -> dict:
    return {
        "account": account,
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "from": gm._header(msg, "From"),
        "to": gm._header(msg, "To"),
        "subject": gm._header(msg, "Subject"),
        "date": gm._header(msg, "Date"),
        "internal_date": datetime.fromtimestamp(
            int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc).isoformat()
        if msg.get("internalDate") else None,
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
        "attachments": [a["filename"] for a in gm.list_attachments(msg)],
        "gmail_url": f"https://mail.google.com/mail/u/?authuser={account}#all/{msg.get('threadId')}",
    }


def _search_one(account: str, query: str, max_results: int) -> dict:
    gm = _gmail()
    svc = gm._service(account)
    res = svc.users().messages().list(
        userId="me", q=query, maxResults=max_results).execute()
    out = []
    for ref in res.get("messages", []) or []:
        msg = svc.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        # format=metadata nao traz partes; anexos ficam vazios aqui, por desenho
        out.append(_msg_meta(gm, account, msg))
    return {"account": account, "count": len(out),
            "estimated_total": res.get("resultSizeEstimate"), "messages": out}


def _thread_full(account: str, thread_id: str, max_chars: int) -> dict:
    gm = _gmail()
    svc = gm._service(account)
    th = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = []
    for m in th.get("messages", []) or []:
        meta = _msg_meta(gm, account, m)
        meta["message_id_header"] = gm._header(m, "Message-ID")
        meta["cc"] = gm._header(m, "Cc")
        meta["body"] = _extract_body_full(m, max_chars)
        meta["attachments"] = gm.list_attachments(m)
        msgs.append(meta)
    return {"account": account, "thread_id": thread_id, "count": len(msgs), "messages": msgs}


def _message_full(account: str, message_id: str, max_chars: int) -> dict:
    gm = _gmail()
    svc = gm._service(account)
    m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    meta = _msg_meta(gm, account, m)
    meta["message_id_header"] = gm._header(m, "Message-ID")
    meta["cc"] = gm._header(m, "Cc")
    meta["body"] = _extract_body_full(m, max_chars)
    meta["attachments"] = gm.list_attachments(m)
    return meta


def _attachment(account: str, message_id: str, attachment_id: str,
                filename: str, as_base64: bool) -> dict:
    gm = _gmail()
    data = gm.download_attachment(account, message_id, attachment_id)
    if len(data) > MAX_ATTACHMENT_BYTES:
        return {"filename": filename, "size": len(data),
                "error": f"anexo acima de {MAX_ATTACHMENT_BYTES} bytes"}
    out: dict[str, Any] = {"filename": filename, "size": len(data)}
    low = (filename or "").lower()
    if low.endswith(".pdf") or data[:5] == b"%PDF-":
        out["text"] = _pdf_text(data)
    elif low.endswith((".txt", ".csv", ".md", ".json", ".xml", ".html", ".htm")):
        txt = data.decode("utf-8", errors="replace")
        out["text"] = _html_to_text(txt) if low.endswith((".html", ".htm")) else txt[:60000]
    else:
        out["note"] = "tipo binario; pedir as_base64=true para obter o conteudo"
    if as_base64:
        out["base64"] = base64.b64encode(data).decode("ascii")
    return out


# --------------------------------------------------------- tool table

TOOLS: list[dict] = [
    {
        "name": "list_accounts",
        "description": "Lista as contas Gmail disponiveis nesta stack e testa a ligacao de cada uma (perfil Gmail). Chamar primeiro quando nao se sabe que contas existem.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_mail",
        "description": "Pesquisa mensagens com sintaxe de pesquisa do Gmail (from:, subject:, newer_than:7d, has:attachment, etc.) numa conta ou em todas ('all'). Devolve metadados e snippet; usar get_thread para o corpo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query Gmail, ex.: 'from:eugest.pt newer_than:30d'"},
                "account": {"type": "string", "description": "Email da conta ou 'all' (predefinido)", "default": "all"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_thread",
        "description": "Devolve todas as mensagens de uma conversa Gmail com corpo (texto) e lista de anexos (com attachment_id para read_attachment).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "thread_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 20000, "minimum": 500, "maximum": 200000},
            },
            "required": ["account", "thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_message",
        "description": "Devolve uma mensagem Gmail com corpo e anexos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "message_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 20000, "minimum": 500, "maximum": 200000},
            },
            "required": ["account", "message_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_attachment",
        "description": "Descarrega um anexo de uma mensagem. PDFs e ficheiros de texto vem com o texto extraido; outros tipos so com as_base64=true. Limite 8 MB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "filename": {"type": "string", "description": "Nome do ficheiro, para escolher o extractor"},
                "as_base64": {"type": "boolean", "default": False},
            },
            "required": ["account", "message_id", "attachment_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_draft",
        "description": "Cria um RASCUNHO no Gmail da conta indicada (nunca envia). Para responder numa conversa, passar thread_id e in_reply_to (header Message-ID da mensagem original).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Texto simples"},
                "thread_id": {"type": "string"},
                "in_reply_to": {"type": "string"},
            },
            "required": ["account", "to", "subject", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "email_inbox_pending",
        "description": "Cartoes de email pendentes/drafted detectados pelo email-scan (tabela email_inbox), opcionalmente por conta.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 300},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "faturas_resumo",
        "description": "Contagens e totais de faturas por mes e empresa (por_validar, validadas, entregues, ignoradas, EUR, USD).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_faturas",
        "description": "Lista faturas do indice. mes 'YYYY-MM'; empresa OMNAI|Previnsa|JMSoares|Sopato|Pessoal; estado por_validar|validada|ignorada|entregue|todos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string"},
                "empresa": {"type": "string"},
                "estado": {"type": "string", "default": "todos"},
                "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_fatura",
        "description": "Texto extraido do PDF de uma fatura do indice (pelo id).",
        "inputSchema": {
            "type": "object",
            "properties": {"fatura_id": {"type": "string"}},
            "required": ["fatura_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fatura_accao",
        "description": "Altera o estado de uma fatura: validar, ignorar, reabrir, ou empresa (reatribuir). Escrita.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fatura_id": {"type": "string"},
                "accao": {"type": "string", "enum": ["validar", "ignorar", "reabrir", "empresa"]},
                "empresa": {"type": "string"},
                "motivo": {"type": "string"},
            },
            "required": ["fatura_id", "accao"],
            "additionalProperties": False,
        },
    },
    {
        "name": "faturas_pacote",
        "description": "Pre-visualizacao do pacote para a contabilidade: faturas validadas de uma empresa num mes, com totais.",
        "inputSchema": {
            "type": "object",
            "properties": {"empresa": {"type": "string"}, "mes": {"type": "string"}},
            "required": ["empresa", "mes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_hoje",
        "description": "Fila do ecra Hoje: itens abertos de briefing_items ordenados por urgencia (P0..P3) e espera.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 60, "minimum": 1, "maximum": 300}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tarefas",
        "description": "Tarefas (user_todos) do ecra Fazer.",
        "inputSchema": {
            "type": "object",
            "properties": {"incluir_feitas": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
    },
    {
        "name": "criar_tarefa",
        "description": "Cria uma tarefa no ecra Fazer a partir de uma linha de texto (interpreta prioridade, empresa e prazo). Escrita.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string"},
                "empresa": {"type": "string"},
                "prazo": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["texto"],
            "additionalProperties": False,
        },
    },
    {
        "name": "empresas_dados",
        "description": "Dados de identificacao das empresas (NIF, morada, contabilista, bancos...) guardados na app.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


# ------------------------------------------------------ tool handlers

async def _t_list_accounts(_: dict) -> Any:
    gm = _gmail()
    res = await asyncio.gather(
        *[asyncio.to_thread(gm.test_connection, a) for a in _accounts()],
        return_exceptions=True)
    out = []
    for a, r in zip(_accounts(), res):
        if isinstance(r, Exception):
            r = {"ok": False, "error": f"{type(r).__name__}: {r}"}
        out.append({"account": a, **r})
    return {"accounts": out, "note": "Previnsa chega por reencaminhamento em opaidapetinga@gmail.com; sapo.pt (IMAP) nao esta exposto aqui."}


async def _t_search_mail(a: dict) -> Any:
    accounts = _resolve_accounts(a.get("account"))
    n = int(a.get("max_results") or 15)
    res = await asyncio.gather(
        *[asyncio.to_thread(_search_one, acc, a["query"], n) for acc in accounts],
        return_exceptions=True)
    out = []
    for acc, r in zip(accounts, res):
        if isinstance(r, Exception):
            out.append({"account": acc, "error": f"{type(r).__name__}: {r}", "messages": []})
        else:
            out.append(r)
    return {"query": a["query"], "results": out}


async def _t_get_thread(a: dict) -> Any:
    _resolve_accounts(a["account"])
    return await asyncio.to_thread(_thread_full, a["account"], a["thread_id"],
                                   int(a.get("max_chars") or 20000))


async def _t_get_message(a: dict) -> Any:
    _resolve_accounts(a["account"])
    return await asyncio.to_thread(_message_full, a["account"], a["message_id"],
                                   int(a.get("max_chars") or 20000))


async def _t_read_attachment(a: dict) -> Any:
    _resolve_accounts(a["account"])
    return await asyncio.to_thread(
        _attachment, a["account"], a["message_id"], a["attachment_id"],
        a.get("filename") or "", bool(a.get("as_base64")))


async def _t_create_draft(a: dict) -> Any:
    _resolve_accounts(a["account"])
    gm = _gmail()
    d = await asyncio.to_thread(
        gm.create_draft, a["account"], a["to"], a["subject"], a["body"],
        a.get("thread_id"), a.get("in_reply_to"))
    return {"draft_id": d.get("id"), "message": d.get("message"),
            "note": "Rascunho criado; nada foi enviado."}


async def _t_email_inbox_pending(a: dict) -> Any:
    from services import email_inbox_db
    rows = await email_inbox_db.list_pending(a.get("account"), int(a.get("limit") or 50))
    return {"count": len(rows), "items": rows}


async def _t_faturas_resumo(_: dict) -> Any:
    from services import faturas_db
    return {"linhas": await faturas_db.resumo()}


async def _t_list_faturas(a: dict) -> Any:
    from services import faturas_db
    estado = a.get("estado") or "todos"
    rows = await faturas_db.listar(mes=a.get("mes"), empresa=a.get("empresa"),
                                   estado=estado, limit=int(a.get("limit") or 200))
    return {"count": len(rows), "empresas": list(faturas_db.EMPRESAS), "itens": rows}


async def _t_read_fatura(a: dict) -> Any:
    from services import faturas_db
    pool = await faturas_db._get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, empresa, fornecedor, data_fatura, valor, moeda, ficheiro, estado "
            "FROM faturas WHERE id = $1::uuid", a["fatura_id"])
    if not row:
        return {"error": "fatura nao encontrada"}
    d = dict(row)
    raiz = FATURAS_DIR.resolve()
    caminho = (raiz / (d.get("ficheiro") or "")).resolve()
    if not d.get("ficheiro") or not str(caminho).startswith(str(raiz)) or not caminho.is_file():
        d["error"] = "ficheiro indisponivel"
        return d
    data = await asyncio.to_thread(caminho.read_bytes)
    d["text"] = await asyncio.to_thread(_pdf_text, data) if caminho.suffix.lower() == ".pdf" \
        else data.decode("utf-8", errors="replace")[:60000]
    return d


async def _t_fatura_accao(a: dict) -> Any:
    from services import faturas_db
    ok = await faturas_db.accao(a["fatura_id"], a["accao"],
                                empresa=a.get("empresa"), motivo=a.get("motivo"))
    return {"ok": True, "aplicado": bool(ok)}


async def _t_faturas_pacote(a: dict) -> Any:
    from services import faturas_db
    itens = await faturas_db.para_pacote(a["empresa"], a["mes"])
    eur = sum(float(i["valor"] or 0) for i in itens if i["moeda"] == "EUR")
    usd = sum(float(i["valor"] or 0) for i in itens if i["moeda"] == "USD")
    return {"empresa": a["empresa"], "mes": a["mes"], "n": len(itens),
            "total_eur": round(eur, 2), "total_usd": round(usd, 2), "itens": itens}


async def _t_list_hoje(a: dict) -> Any:
    from services import briefing_db
    rows = await briefing_db.list_hoje(int(a.get("limit") or 60))
    for r in rows:
        if isinstance(r.get("metadata"), str):
            try:
                r["metadata"] = json.loads(r["metadata"])
            except Exception:
                pass
    return {"count": len(rows), "items": rows}


async def _t_list_tarefas(a: dict) -> Any:
    from services import fazer_db
    rows = await fazer_db.listar(incluir_feitas=bool(a.get("incluir_feitas")))
    return {"count": len(rows), "items": rows}


async def _t_criar_tarefa(a: dict) -> Any:
    from services import fazer_db
    prazo = date.fromisoformat(a["prazo"]) if a.get("prazo") else None
    return await fazer_db.criar(a["texto"], empresa=a.get("empresa"), prazo=prazo)


async def _t_empresas_dados(_: dict) -> Any:
    from services import empresas_db
    rows = await empresas_db.listar()
    por_empresa: dict[str, list] = {}
    for r in rows:
        por_empresa.setdefault(r["empresa"], []).append(
            {"campo": r["campo"], "valor": r["valor"], "nota": r.get("nota")})
    return por_empresa


HANDLERS = {
    "list_accounts": _t_list_accounts,
    "search_mail": _t_search_mail,
    "get_thread": _t_get_thread,
    "get_message": _t_get_message,
    "read_attachment": _t_read_attachment,
    "create_draft": _t_create_draft,
    "email_inbox_pending": _t_email_inbox_pending,
    "faturas_resumo": _t_faturas_resumo,
    "list_faturas": _t_list_faturas,
    "read_fatura": _t_read_fatura,
    "fatura_accao": _t_fatura_accao,
    "faturas_pacote": _t_faturas_pacote,
    "list_hoje": _t_list_hoje,
    "list_tarefas": _t_list_tarefas,
    "criar_tarefa": _t_criar_tarefa,
    "empresas_dados": _t_empresas_dados,
}


# ------------------------------------------------------ JSON-RPC core

def _rpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _rpc_result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


async def _handle_one(msg: dict) -> dict | None:
    """Devolve a resposta, ou None para notificacoes."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "pedido invalido")
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if not isinstance(method, str):
        return None if is_notification else _rpc_error(id_, -32600, "method em falta")

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _rpc_result(id_, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Stack OMNAI do David Sardinha. Contas Gmail: OMNAI "
                "(david.sardinha@omnai.pt), pessoal (davidsardinhalves@gmail.com), "
                "Sopato (sopato.cascais@gmail.com), Previnsa por reencaminhamento "
                "(opaidapetinga@gmail.com). Faturas, fila Hoje e tarefas vem do Postgres "
                "da VPS. create_draft nunca envia email."
            ),
        })
    if method == "ping":
        return _rpc_result(id_, {})
    if method == "tools/list":
        return _rpc_result(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _rpc_error(id_, -32602, f"tool desconhecida: {name}")
        try:
            result = await handler(args)
            return _rpc_result(id_, _text_result(result))
        except (KeyError, ValueError) as exc:
            return _rpc_result(id_, _text_result(
                {"error": f"argumentos invalidos: {type(exc).__name__}: {exc}"}, True))
        except Exception as exc:
            log.exception("mcp.tool_error", tool=name)
            return _rpc_result(id_, _text_result(
                {"error": f"{type(exc).__name__}: {exc}"}, True))
    if method in ("resources/list", "resources/templates/list"):
        key = "resourceTemplates" if method.endswith("templates/list") else "resources"
        return _rpc_result(id_, {key: []})
    if method == "prompts/list":
        return _rpc_result(id_, {"prompts": []})
    if is_notification:
        return None
    return _rpc_error(id_, -32601, f"metodo desconhecido: {method}")


async def _handle_post(request: Request, path_token: str | None) -> Response:
    if not _authorized(request, path_token):
        return _unauthorized()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "JSON invalido"), status_code=400)

    headers = {}
    if isinstance(payload, dict) and payload.get("method") == "initialize":
        headers["Mcp-Session-Id"] = secrets.token_hex(16)

    if isinstance(payload, list):
        responses = [r for r in [await _handle_one(m) for m in payload] if r is not None]
        if not responses:
            return Response(status_code=202, headers=headers)
        return JSONResponse(responses, headers=headers)

    resp = await _handle_one(payload)
    if resp is None:
        return Response(status_code=202, headers=headers)
    return JSONResponse(resp, headers=headers)


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    return await _handle_post(request, None)


@router.post("/mcp/{path_token}")
async def mcp_post_token(request: Request, path_token: str) -> Response:
    return await _handle_post(request, path_token)


@router.get("/mcp")
@router.get("/mcp/{path_token}")
async def mcp_get(request: Request, path_token: str | None = None) -> Response:
    """Sem canal servidor -> cliente. O cliente usa so POST."""
    if not _authorized(request, path_token):
        return _unauthorized()
    return JSONResponse({"error": "sem stream SSE; usar POST"}, status_code=405,
                        headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
@router.delete("/mcp/{path_token}")
async def mcp_delete(request: Request, path_token: str | None = None) -> Response:
    if not _authorized(request, path_token):
        return _unauthorized()
    return Response(status_code=204)
