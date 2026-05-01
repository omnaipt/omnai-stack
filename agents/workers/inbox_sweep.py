"""inbox-sweep v1.1 - worker consolidado Carlos + checkbox auto-archive.

Substitui 4 workers antigos (email-scan, briefing-carlos, email-check-midday,
email-fecho-dia) por 1 modulo com 3 entrypoints, um por fase do dia.

v1.1 adiciona _process_pending_checkboxes() que le o briefing da corrida
anterior, detecta to-dos checked com link /mail/TOKEN e arquiva o email real
(Gmail ou IMAP) antes de regenerar a pagina.

Task IDs / crons:
  * inbox-sweep-morning  - Seg-Sex 08:00
  * inbox-sweep-midday   - Seg-Sex 13:00
  * inbox-sweep-evening  - Seg-Sex 18:00
"""
from __future__ import annotations

import asyncio
import base64
import re
from typing import Any

import structlog

from services import gmail as gmail_svc
from services import imap_client as imap_svc
from services.notion import NotionClient
from services.state import pop_pending_email
from workers import briefing_carlos, email_scan

log = structlog.get_logger()

MORNING_BRIEFING_PAGE_ID = "33e973b9-2387-8107-8667-eadc9128ab27"
MAIL_TOKEN_PREFIX = "https://agents.omnai.pt/mail/"
IMAP_ACCOUNTS = {
    "hello@omnai.pt",
    "david.sardinha@omnai.pt",
    "david.sardinha@sapo.pt",
}


def _decode_token(token: str) -> tuple[str, str] | None:
    try:
        pad = "=" * ((4 - len(token) % 4) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
        if "|" in raw:
            acc, mid = raw.split("|", 1)
            return acc, mid
    except Exception:
        pass
    return None


async def _process_pending_checkboxes() -> dict[str, Any]:
    """Le briefing anterior; para cada to_do checked com link /mail/TOKEN -> archive.

    Suporta Gmail e IMAP. O token encripta "account|message_id" em base64url.
    Remove da fila state apos arquivar com sucesso.
    """
    archived = 0
    failures = 0
    tokens_seen: list[str] = []
    try:
        async with NotionClient() as notion:
            blocks = await notion.get_block_children(MORNING_BRIEFING_PAGE_ID)
    except Exception as exc:
        log.warning("checkbox.read_failed", err=str(exc))
        return {"archived": 0, "failures": 0, "tokens_detected": 0}

    for b in blocks:
        if b.get("type") != "to_do":
            continue
        td = b.get("to_do", {}) or {}
        if not td.get("checked"):
            continue
        token: str | None = None
        for r in td.get("rich_text", []) or []:
            text_obj = r.get("text", {}) or {}
            link_obj = text_obj.get("link") or {}
            url = link_obj.get("url", "") or r.get("href", "") or ""
            if url and "/mail/" in url:
                m = re.search(r"/mail/([A-Za-z0-9_\-]+)", url)
                if m:
                    token = m.group(1)
                    break
        if not token:
            continue
        tokens_seen.append(token)
        decoded = _decode_token(token)
        if not decoded:
            log.warning("checkbox.bad_token", token=token[:20])
            failures += 1
            continue
        account, msg_id = decoded
        try:
            if account in IMAP_ACCOUNTS:
                ok = await asyncio.to_thread(imap_svc.archive_message, account, msg_id)
            else:
                ok = await asyncio.to_thread(gmail_svc.archive_message, account, msg_id)
            if ok:
                archived += 1
                await pop_pending_email(token)
                log.info("checkbox.archived", account=account, msg_id=msg_id)
            else:
                failures += 1
                log.warning("checkbox.archive_failed", account=account, msg_id=msg_id)
        except Exception as exc:
            failures += 1
            log.warning("checkbox.exc", err=str(exc), account=account)

    log.info(
        "checkbox.summary",
        tokens=len(tokens_seen),
        archived=archived,
        failures=failures,
    )
    return {
        "archived": archived,
        "failures": failures,
        "tokens_detected": len(tokens_seen),
    }


async def _run_scan(mode: str) -> dict[str, Any]:
    log.info("inbox_sweep.scan.start", mode=mode)
    res = await email_scan.run()
    log.info("inbox_sweep.scan.done", mode=mode, status=res.get("status"))
    return res


async def run_morning() -> dict[str, Any]:
    """Sweep matinal: processa checkboxes + scan completo + briefing executivo."""
    checkbox_result = await _process_pending_checkboxes()
    scan = await _run_scan("morning")
    log.info("inbox_sweep.briefing.start", mode="morning")
    briefing = await briefing_carlos.run()
    log.info("inbox_sweep.briefing.done", mode="morning", status=briefing.get("status"))
    return {
        "status": "ok",
        "mode": "morning",
        "checkbox": checkbox_result,
        "scan": scan,
        "briefing": briefing,
    }


async def run_midday() -> dict[str, Any]:
    """Sweep meio-dia: checkboxes + refresh inbox + actualiza briefing persistente."""
    checkbox_result = await _process_pending_checkboxes()
    scan = await _run_scan("midday")
    return {
        "status": "ok",
        "mode": "midday",
        "checkbox": checkbox_result,
        "scan": scan,
    }


async def run_evening() -> dict[str, Any]:
    """Sweep fecho-do-dia: checkboxes + scan final. V2: acrescentar plano de amanha."""
    checkbox_result = await _process_pending_checkboxes()
    scan = await _run_scan("evening")
    return {
        "status": "ok",
        "mode": "evening",
        "checkbox": checkbox_result,
        "scan": scan,
        "plano_amanha": "TODO V2",
    }


# Default alias para debug manual
async def run() -> dict[str, Any]:
    return await run_morning()
