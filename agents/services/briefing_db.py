"""Acesso a tabela briefing_items (Postgres).

v9.2.1: nova funcao list_recently_resolved para mostrar 'Resolvido hoje'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not POSTGRES_DSN:
            raise RuntimeError("POSTGRES_DSN/DATABASE_URL nao definido")
        _pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=5)
    return _pool


URGENCIAS = ("P0", "P1", "P2", "P3")
EMPRESAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")


@dataclass
class BriefingItem:
    chave: str
    tipo: str
    titulo: str
    urgencia: str = "P2"
    empresa: str | None = None
    detalhe: str | None = None
    link_origem: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.urgencia not in URGENCIAS:
            raise ValueError(f"urgencia invalida: {self.urgencia}")
        if self.empresa is not None and self.empresa not in EMPRESAS:
            logger.warning("empresa fora do conjunto conhecido: %s", self.empresa)


def make_chave(*parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def upsert_item(item: BriefingItem) -> str:
    pool = await _get_pool()
    metadata_json = json.dumps(item.metadata, ensure_ascii=False) if item.metadata else None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO briefing_items
                (chave, tipo, urgencia, empresa, titulo, detalhe, link_origem, metadata)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (chave) DO UPDATE SET
                tipo       = EXCLUDED.tipo,
                urgencia   = CASE
                                 WHEN briefing_items.status IN ('done', 'dismissed') THEN briefing_items.urgencia
                                 ELSE EXCLUDED.urgencia
                             END,
                empresa    = COALESCE(EXCLUDED.empresa, briefing_items.empresa),
                titulo     = CASE
                                 WHEN briefing_items.status IN ('done', 'dismissed') THEN briefing_items.titulo
                                 ELSE EXCLUDED.titulo
                             END,
                detalhe    = CASE
                                 WHEN briefing_items.status IN ('done', 'dismissed') THEN briefing_items.detalhe
                                 ELSE EXCLUDED.detalhe
                             END,
                link_origem= COALESCE(EXCLUDED.link_origem, briefing_items.link_origem),
                metadata   = COALESCE(EXCLUDED.metadata, briefing_items.metadata)
            RETURNING id::text;
            """,
            item.chave,
            item.tipo,
            item.urgencia,
            item.empresa,
            item.titulo,
            item.detalhe,
            item.link_origem,
            metadata_json,
        )
        return row["id"] if row else ""


async def list_open(limit: int = 200) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM briefing_inbox_view LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]


async def list_recently_resolved(hours: int = 24, limit: int = 50) -> list[dict]:
    """Items resolvidos nas ultimas N horas (status=done ou dismissed).

    Usado pelo briefing-inbox para mostrar a seccao 'Resolvido hoje'.
    """
    pool = await _get_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM briefing_items
             WHERE status IN ('done', 'dismissed')
               AND resolvido_em IS NOT NULL
               AND resolvido_em >= $1
             ORDER BY resolvido_em DESC
             LIMIT $2
            """,
            cutoff,
            limit,
        )
        return [dict(r) for r in rows]


async def mark_done(item_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE briefing_items
               SET status = 'done', resolvido_em = NOW()
             WHERE id = $1::uuid
               AND status IN ('open', 'snoozed')
            """,
            item_id,
        )
        return result.endswith(" 1")


async def mark_done_by_chave(chave: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE briefing_items
               SET status = 'done', resolvido_em = NOW()
             WHERE chave = $1
               AND status IN ('open', 'snoozed')
            """,
            chave,
        )
        return result.endswith(" 1")


async def mark_dismissed(item_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE briefing_items
               SET status = 'dismissed', resolvido_em = NOW()
             WHERE id = $1::uuid
               AND status IN ('open', 'snoozed')
            """,
            item_id,
        )
        return result.endswith(" 1")


async def snooze(item_id: str, days: int = 7) -> bool:
    if days < 1 or days > 60:
        raise ValueError("days deve estar entre 1 e 60")
    pool = await _get_pool()
    until = datetime.now(timezone.utc) + timedelta(days=days)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE briefing_items
               SET status = 'snoozed', snooze_until = $2
             WHERE id = $1::uuid
               AND status IN ('open', 'snoozed')
            """,
            item_id,
            until,
        )
        return result.endswith(" 1")


async def get_by_id(item_id: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM briefing_items WHERE id = $1::uuid",
            item_id,
        )
        return dict(row) if row else None


async def get_by_chave(chave: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM briefing_items WHERE chave = $1",
            chave,
        )
        return dict(row) if row else None


async def stats_por_urgencia() -> dict[str, int]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT urgencia, COUNT(*) AS n
              FROM briefing_inbox_view
             GROUP BY urgencia
            """
        )
        return {r["urgencia"]: r["n"] for r in rows}


async def stats_por_empresa() -> dict[str, int]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(empresa, 'Sem empresa') AS empresa, COUNT(*) AS n
              FROM briefing_inbox_view
             GROUP BY empresa
            """
        )
        return {r["empresa"]: r["n"] for r in rows}
