"""Estado partilhado via Redis: email stats, drafts, faturas.

Sprint 9: nova funcao pop_draft_by_id para apagar draft individual quando
o David clica [Marcado respondido] no card do briefing.
"""
from __future__ import annotations

import json
import os
from typing import Any

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "")
_client: aioredis.Redis | None = None


def _c() -> aioredis.Redis | None:
    global _client
    if _client is None and REDIS_URL:
        _client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _client


# ---- Email stats ----

async def set_email_stats(account: str, stats: dict[str, Any]) -> None:
    c = _c()
    if c is None:
        return
    key = f"omnai:email:stats:{account}"
    await c.hset(key, mapping={k: str(v) for k, v in stats.items()})
    await c.expire(key, 60 * 60 * 36)


async def get_all_email_stats() -> dict[str, dict[str, str]]:
    c = _c()
    if c is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    prefix = "omnai:email:stats:"
    async for key in c.scan_iter(match=f"{prefix}*"):
        account = key[len(prefix):]
        out[account] = await c.hgetall(key)
    return out


# ---- Drafts de resposta ----

DRAFTS_KEY = "omnai:email:drafts"


async def push_draft(draft: dict[str, Any]) -> None:
    c = _c()
    if c is None:
        return
    await c.lpush(DRAFTS_KEY, json.dumps(draft, ensure_ascii=False))
    await c.ltrim(DRAFTS_KEY, 0, 49)
    await c.expire(DRAFTS_KEY, 60 * 60 * 36)


async def peek_drafts() -> list[dict[str, Any]]:
    c = _c()
    if c is None:
        return []
    raw = await c.lrange(DRAFTS_KEY, 0, -1)
    return [json.loads(x) for x in raw]


async def pop_drafts() -> list[dict[str, Any]]:
    c = _c()
    if c is None:
        return []
    raw = await c.lrange(DRAFTS_KEY, 0, -1)
    await c.delete(DRAFTS_KEY)
    return [json.loads(x) for x in raw]


async def pop_draft_by_id(draft_id: str) -> dict[str, Any] | None:
    """Sprint 9: remove draft individual de Redis pelo draft_id.

    Devolve dict com {draft_id, gmail_message_id, account/inbox, ...} ou None
    se nao encontrar. Usado por /actions/draft-done.
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
            await c.lrem(DRAFTS_KEY, 1, item)
            return d
    return None


# ---- Faturas arquivadas hoje ----

async def push_invoice(entry: dict[str, Any]) -> None:
    c = _c()
    if c is None:
        return
    await c.lpush("omnai:invoices:archived_today", json.dumps(entry, ensure_ascii=False))
    await c.ltrim("omnai:invoices:archived_today", 0, 99)
    await c.expire("omnai:invoices:archived_today", 60 * 60 * 36)


async def peek_invoices() -> list[dict[str, Any]]:
    c = _c()
    if c is None:
        return []
    raw = await c.lrange("omnai:invoices:archived_today", 0, -1)
    return [json.loads(x) for x in raw]


# ---- Faturas que precisam de extraccao manual ----

async def push_manual_invoice(entry: dict[str, Any]) -> None:
    c = _c()
    if c is None:
        return
    await c.lpush("omnai:invoices:manual_queue", json.dumps(entry, ensure_ascii=False))
    await c.ltrim("omnai:invoices:manual_queue", 0, 99)
    await c.expire("omnai:invoices:manual_queue", 60 * 60 * 72)


async def peek_manual_invoices() -> list[dict[str, Any]]:
    c = _c()
    if c is None:
        return []
    raw = await c.lrange("omnai:invoices:manual_queue", 0, -1)
    return [json.loads(x) for x in raw]


# ---- Emails pendentes para auto-archive via checkbox (v0.6.0) ----

async def push_pending_email(entry: dict[str, Any]) -> None:
    c = _c()
    if c is None: return
    await c.lpush("omnai:email:pending", json.dumps(entry, ensure_ascii=False))
    await c.ltrim("omnai:email:pending", 0, 199)
    await c.expire("omnai:email:pending", 60 * 60 * 72)


async def peek_pending_emails() -> list[dict[str, Any]]:
    c = _c()
    if c is None: return []
    raw = await c.lrange("omnai:email:pending", 0, -1)
    return [json.loads(x) for x in raw]


async def pop_pending_email(token: str) -> bool:
    """Remove um pending email por token. Devolve True se encontrou."""
    c = _c()
    if c is None: return False
    raw = await c.lrange("omnai:email:pending", 0, -1)
    for item in raw:
        try:
            d = json.loads(item)
            if d.get("token") == token:
                await c.lrem("omnai:email:pending", 1, item)
                return True
        except Exception:
            continue
    return False
