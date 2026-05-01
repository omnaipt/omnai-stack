"""Cliente Gmail v0.5.0 - adiciona download_attachments() e list_attachments()."""
from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = structlog.get_logger()

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
TOKENS_DIR = SECRETS_DIR / "tokens"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

GMAIL_ACCOUNTS: dict[str, str] = {
    "davidsardinhalves@gmail.com": "davidsardinhalves_at_gmail_com",
    "sopato.cascais@gmail.com": "sopato_cascais_at_gmail_com",
    "opaidapetinga@gmail.com": "opaidapetinga_at_gmail_com",
}


def _token_path(account: str) -> Path:
    stem = GMAIL_ACCOUNTS.get(account)
    if not stem:
        raise ValueError(f"Conta Gmail nao conhecida: {account}")
    return TOKENS_DIR / f"{stem}.json"


def _load_creds(account: str) -> Credentials:
    path = _token_path(account)
    if not path.exists():
        raise FileNotFoundError(f"Token nao encontrado em {path}")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json())
        else:
            raise RuntimeError(f"Token invalido para {account}")
    return creds


def _service(account: str):
    return build("gmail", "v1", credentials=_load_creds(account), cache_discovery=False)


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_body(msg: dict, max_chars: int = 5000) -> str:
    def walk(part: dict) -> str:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    return ""
        for sub in part.get("parts", []) or []:
            r = walk(sub)
            if r:
                return r
        return ""

    body = walk(msg.get("payload", {}))
    if not body:
        data = msg.get("payload", {}).get("body", {}).get("data")
        if data:
            try:
                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                body = ""
    return body[:max_chars]


def _walk_attachments(part: dict, out: list[dict]) -> None:
    """Colecciona recursivamente partes com attachmentId."""
    filename = part.get("filename") or ""
    body = part.get("body", {}) or {}
    att_id = body.get("attachmentId")
    if filename and att_id:
        out.append({
            "filename": filename,
            "mime_type": part.get("mimeType", ""),
            "attachment_id": att_id,
            "size": body.get("size", 0),
        })
    for sub in part.get("parts", []) or []:
        _walk_attachments(sub, out)


def list_attachments(msg: dict) -> list[dict]:
    out: list[dict] = []
    _walk_attachments(msg.get("payload", {}), out)
    return out


def download_attachment(account: str, message_id: str, attachment_id: str) -> bytes:
    svc = _service(account)
    att = svc.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()
    data = att.get("data", "")
    return base64.urlsafe_b64decode(data)


def list_inbox_messages(account: str, hours: int = 24, cap: int = 50) -> list[dict]:
    svc = _service(account)
    try:
        result = svc.users().messages().list(
            userId="me", q="in:inbox", maxResults=cap
        ).execute()
    except HttpError as exc:
        log.warning("gmail.list_failed", account=account, err=str(exc))
        return []
    return result.get("messages", [])


def summarize_message(account: str, message_id: str) -> dict:
    svc = _service(account)
    try:
        msg = svc.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as exc:
        log.warning("gmail.get_failed", account=account, mid=message_id, err=str(exc))
        return {}

    atts = list_attachments(msg)
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "subject": _header(msg, "Subject"),
        "date": _header(msg, "Date"),
        "message_id_header": _header(msg, "Message-ID"),
        "body": _extract_body(msg),
        "snippet": msg.get("snippet", ""),
        "labels": msg.get("labelIds", []),
        "internal_date_ms": int(msg.get("internalDate", 0)),
        "attachments": atts,
    }


def archive_message(account: str, message_id: str) -> bool:
    svc = _service(account)
    try:
        svc.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
        ).execute()
        return True
    except HttpError as exc:
        log.warning("gmail.archive_failed", account=account, err=str(exc))
        return False


def trash_message(account: str, message_id: str) -> bool:
    svc = _service(account)
    try:
        svc.users().messages().trash(userId="me", id=message_id).execute()
        return True
    except HttpError as exc:
        log.warning("gmail.trash_failed", account=account, err=str(exc))
        return False


def create_draft(
    account: str,
    to_addr: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
) -> dict:
    svc = _service(account)
    message = MIMEText(body, _charset="utf-8")
    message["to"] = to_addr
    message["from"] = account
    message["subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    payload: dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id

    try:
        return svc.users().drafts().create(userId="me", body=payload).execute()
    except HttpError as exc:
        log.warning("gmail.draft_failed", account=account, err=str(exc))
        raise


def get_message_url(account: str, message_id: str) -> str:
    """URL para abrir mensagem directamente no Gmail UI."""
    return f"https://mail.google.com/mail/u/?authuser={account}#all/{message_id}"


def test_connection(account: str) -> dict:
    try:
        svc = _service(account)
        profile = svc.users().getProfile(userId="me").execute()
        return {
            "ok": True,
            "email": profile.get("emailAddress"),
            "messages_total": profile.get("messagesTotal"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
