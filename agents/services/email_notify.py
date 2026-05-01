"""Envio de emails transaccionais internos (notificacoes ao David) via
Gmail API, reutilizando o _service(account) ja existente em services.gmail.

Usamos a conta davidsardinhalves@gmail.com (com scope gmail.modify que
permite send) como remetente de sistema.

Interface (async):
    await send_notification(subject=, html_body=, to=, cc=, account=)
"""
from __future__ import annotations

import asyncio
import base64
import logging
from email.message import EmailMessage

from services.gmail import _service, GMAIL_ACCOUNTS

logger = logging.getLogger(__name__)


SYSTEM_ACCOUNT = "davidsardinhalves@gmail.com"
DEFAULT_RECIPIENT = "davidsardinhalves@gmail.com"


def _build_raw(*, sender: str, to: str, subject: str, html_body: str, cc: str | None = None) -> str:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content("Esta mensagem requer um cliente com suporte HTML.")
    msg.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def _send_sync(*, account: str, to: str, subject: str, html_body: str, cc: str | None) -> dict | None:
    """Versao sincrona (googleapiclient e sincrono)."""
    if account not in GMAIL_ACCOUNTS:
        logger.warning("email_notify: conta %s nao configurada", account)
        return None
    try:
        svc = _service(account)
        raw = _build_raw(sender=account, to=to, subject=subject, html_body=html_body, cc=cc)
        return svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("email_notify FAIL account=%s to=%s err=%s", account, to, exc)
        return None


async def send_notification(
    *,
    subject: str,
    html_body: str,
    to: str = DEFAULT_RECIPIENT,
    cc: str | None = None,
    account: str = SYSTEM_ACCOUNT,
) -> dict | None:
    """Envia notificacao interna. Async-friendly via run_in_executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _send_sync(account=account, to=to, subject=subject, html_body=html_body, cc=cc),
    )
