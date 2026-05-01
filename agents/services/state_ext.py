"""Extensoes async ao services.state para funcionalidades da v9.1.

Funcoes novas:
- incr_rate_limit(domain) -> int: incrementa contador diario
- get_content_hash / set_content_hash: para analise competitiva
- peek_invoices_all() / peek_manual_invoices_all(): alias publico

Reutiliza o mesmo cliente redis de services.state._c().
"""
from __future__ import annotations

from datetime import date, timedelta

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
