"""HMAC-signed tokens para os links 'Resolver' e 'Snooze' nos cards.

Cada item gera tokens unicos para 3 accoes (done, snooze1d, snooze7d).
Os links sao GET (clicáveis em qualquer browser, incluindo do mobile),
mas o token impede que terceiros marquem itens dos outros so adivinhando UUIDs.

Token: HMAC-SHA256(SECRET, f"{item_id}|{action}|{days}|{exp}")
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

ACTION_SECRET = os.getenv("ACTION_TOKEN_SECRET") or os.getenv("OMNAI_API_TOKEN") or ""
TOKEN_TTL_SEC = 60 * 60 * 24 * 30  # 30 dias


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_token(item_id: str, action: str, days: int = 0, ttl_sec: int = TOKEN_TTL_SEC) -> str:
    if not ACTION_SECRET:
        raise RuntimeError("ACTION_TOKEN_SECRET / OMNAI_API_TOKEN nao definidos")
    exp = int(time.time()) + ttl_sec
    payload = f"{item_id}|{action}|{days}|{exp}"
    sig = hmac.new(ACTION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{exp}.{days}.{_b64(sig)}"


def verify_token(item_id: str, action: str, token: str) -> tuple[bool, int]:
    """Devolve (ok, days). days e 0 para 'done'/'dismissed', N para snooze."""
    if not ACTION_SECRET:
        return False, 0
    try:
        exp_str, days_str, sig_b64 = token.split(".", 2)
        exp = int(exp_str)
        days = int(days_str)
    except Exception:
        return False, 0

    if exp < int(time.time()):
        return False, days

    expected = hmac.new(
        ACTION_SECRET.encode("utf-8"),
        f"{item_id}|{action}|{days}|{exp}".encode("utf-8"),
        hashlib.sha256,
    ).digest()

    try:
        provided = _b64d(sig_b64)
    except Exception:
        return False, days

    if not hmac.compare_digest(expected, provided):
        return False, days

    return True, days


def link_for(base_url: str, item_id: str, action: str, days: int = 0) -> str:
    """Constroi link absoluto para incluir no card Notion.

    Exemplo:
        link_for('https://agents.omnai.pt', '<uuid>', 'done')
        -> 'https://agents.omnai.pt/actions/done?id=<uuid>&t=<token>'
    """
    token = make_token(item_id, action, days=days)
    return f"{base_url.rstrip('/')}/actions/{action}?id={item_id}&t={token}&d={days}"
