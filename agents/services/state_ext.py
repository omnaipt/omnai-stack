"""Extensoes async ao services.state.

v9.1: incr_rate_limit, content_hash helpers, peek_archived/manual.
Sprint 9: pop_draft_by_id (apaga draft individual de Redis pelo draft_id).

Reutiliza o mesmo cliente redis de services.state._c().
"""
from __future__ import annotations

import json
from datetime import date

from services.state import _c


RATE_LIMIT_TTL_SEC = 48 * 60 * 60
CONTENT_HASH_TTL_SEC = 60 * 60 * 24 * 90


async def incr_rate_limit(domain: str) -> int:
    """Incrementa contador do dia para dominio e devolve novo valor.

    Retorna 0 se Redis nao disponivel (fail-open).
    """
    c = _c()
    if c is None:
        return 0
    key = f"omnai:email:rate:{date.today().isoformat()}:{domain.lower()}"
    count = await c.incr(key)
    if count == 1:
        await c.expire(key, RATE_LIMIT_TTL_SEC)
    return int(count)


async def get_content_hash(cid: str, path: str) -> str | None:
    c = _c()
    if c is None:
        return None
    value = await c.get(f"omnai:competitors:hash:{cid}:{path}")
    return value


async def set_content_hash(cid: str, path: str, sha: str) -> None:
    c = _c()
    if c is None:
        return
    await c.set(f"omnai:competitors:hash:{cid}:{path}", sha, ex=CONTENT_HASH_TTL_SEC)


async def peek_archived_today() -> list[str]:
    """Devolve lista raw (JSON strings) das faturas arquivadas hoje."""
    c = _c()
    if c is None:
        return []
    return await c.lrange("omnai:invoices:archived_today", 0, -1)


async def peek_manual_queue() -> list[str]:
    """Devolve lista raw (JSON strings) das faturas em manual queue."""
    c = _c()
    if c is None:
        return []
    return await c.lrange("omnai:invoices:manual_queue", 0, -1)


# ---- Sprint 9: pop draft individual ----

DRAFTS_KEY = "omnai:email:drafts"


async def pop_draft_by_id(draft_id: str) -> dict | None:
    """Remove um draft especifico da lista de drafts em Redis.

    Devolve o dict do draft removido (com gmail_message_id, account, etc.)
    ou None se nao encontrar.

    Usado pelo endpoint /actions/draft-done quando o David clica
    [Marcado respondido] num card de draft.
    """
    c = _c()
    if c is None or not draft_id:
        return None
    raw = await c.lrange(DRAFTS_KEY, 0, -1)
    for item in raw:
        try:
            d = json.loads(item)
        except Exception:
            continue
        if d.get("draft_id") == draft_id:
            # LREM exact match: o redis-py compara byte-a-byte com o valor
            # original retornado por LRANGE.
            await c.lrem(DRAFTS_KEY, 1, item)
            return d
    return None
