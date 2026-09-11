"""CRUD para email_inbox (emails actionable detectados pelo email-scan)."""
from __future__ import annotations

import hashlib
from typing import Any

from services.briefing_db import _get_pool


def make_email_chave(account: str, message_id: str) -> str:
    raw = f"{account}|{message_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def upsert(
    *, account: str, message_id: str, thread_id: str | None,
    from_addr: str | None, subject: str | None, snippet: str | None,
    classificacao: str = "actionable",
    gmail_thread_url: str | None = None,
) -> str:
    chave = make_email_chave(account, message_id)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO email_inbox
              (chave, message_id, thread_id, account, from_addr, subject, snippet, classificacao, gmail_thread_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (chave) DO UPDATE SET
              subject = EXCLUDED.subject,
              snippet = COALESCE(EXCLUDED.snippet, email_inbox.snippet),
              from_addr = COALESCE(EXCLUDED.from_addr, email_inbox.from_addr),
              thread_id = COALESCE(EXCLUDED.thread_id, email_inbox.thread_id),
              gmail_thread_url = COALESCE(EXCLUDED.gmail_thread_url, email_inbox.gmail_thread_url)
            RETURNING id::text
            """,
            chave, message_id, thread_id, account, from_addr, subject, snippet,
            classificacao, gmail_thread_url,
        )
        return row["id"] if row else ""


async def list_pending(account: str | None = None, limit: int = 100) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if account:
            rows = await conn.fetch(
                """
                SELECT * FROM email_inbox
                 WHERE status IN ('pending', 'drafted')
                   AND account = $1
                 ORDER BY criado_em DESC
                 LIMIT $2
                """,
                account, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM email_inbox
                 WHERE status IN ('pending', 'drafted')
                 ORDER BY criado_em DESC
                 LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]


async def get_by_id(item_id: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM email_inbox WHERE id = $1::uuid", item_id,
        )
        return dict(row) if row else None


async def mark_done(item_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE email_inbox
               SET status = 'done', resolvido_em = NOW()
             WHERE id = $1::uuid AND status IN ('pending', 'drafted')
            """,
            item_id,
        )
        return res.endswith(" 1")


async def mark_replied(item_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE email_inbox
               SET status = 'replied', resolvido_em = NOW()
             WHERE id = $1::uuid AND status IN ('pending', 'drafted')
            """,
            item_id,
        )
        return res.endswith(" 1")


async def mark_dismissed(item_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE email_inbox
               SET status = 'dismissed', resolvido_em = NOW()
             WHERE id = $1::uuid AND status IN ('pending', 'drafted')
            """,
            item_id,
        )
        return res.endswith(" 1")


async def save_draft(item_id: str, draft_text: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE email_inbox
               SET status = 'drafted',
                   draft_text = $2,
                   draft_generated_at = NOW()
             WHERE id = $1::uuid AND status IN ('pending', 'drafted')
            """,
            item_id, draft_text,
        )
        return res.endswith(" 1")


async def summary_por_caixa() -> dict:
    """Stats por conta: total pending, drafted, done last 24h."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              account,
              SUM(CASE WHEN status = 'pending'  THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status = 'drafted'  THEN 1 ELSE 0 END) AS drafted,
              SUM(CASE WHEN status IN ('done','replied','dismissed')
                       AND resolvido_em >= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END) AS resolved_24h
            FROM email_inbox
            GROUP BY account
            """
        )
        return {r["account"]: dict(r) for r in rows}


async def total_pending() -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM email_inbox WHERE status IN ('pending', 'drafted')"
        )
        return row["n"] if row else 0
