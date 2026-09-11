"""gmail_archive: arquivar emails no Gmail via OAuth (Sprint 9).

Move email da INBOX para label OMNAI-Processed (cria label se nao existir).
Wrapper async sobre os helpers sync do services.gmail e services.gmail_labels.

Uso pelo main.py nos endpoints /actions/done, /actions/dismiss e
/actions/draft-done quando o card / draft tem metadata Gmail.
"""
from __future__ import annotations

import asyncio

import structlog

from services.gmail import _service
from services.gmail_labels import PROCESSED_LABEL_NAME, ensure_label

log = structlog.get_logger()

PROCESSED_LABEL = PROCESSED_LABEL_NAME  # "OMNAI-Processed"


def _archive_sync(inbox: str, message_id: str) -> tuple[bool, str]:
    """Versao sync (corre em thread): build service, ensure label, modify."""
    try:
        svc = _service(inbox)
        label_id = ensure_label(svc, PROCESSED_LABEL)

        body = {
            "removeLabelIds": ["INBOX"],
            "addLabelIds": [label_id],
        }
        svc.users().messages().modify(
            userId="me",
            id=message_id,
            body=body,
        ).execute()

        log.info("gmail_archive.ok", inbox=inbox, message_id=message_id[:12])
        return True, "archived"
    except Exception as exc:
        log.warning("gmail_archive.fail", inbox=inbox, msg=str(exc))
        return False, f"{type(exc).__name__}: {exc}"


async def archive_message(inbox: str, message_id: str) -> tuple[bool, str]:
    """Arquiva mensagem Gmail.

    Args:
        inbox: email account (ex: davidsardinhalves@gmail.com).
        message_id: Gmail message ID (NAO thread ID).

    Returns:
        (success, msg).

    Nota: `services.gmail._service` e sync (googleapiclient nao tem variante
    async oficial). Usamos asyncio.to_thread para nao bloquear o event loop.
    """
    if not inbox or not message_id:
        return False, "missing inbox or message_id"
    return await asyncio.to_thread(_archive_sync, inbox, message_id)
