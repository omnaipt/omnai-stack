"""CRUD para user_todos (tarefas pessoais criadas manualmente na UI)."""
from __future__ import annotations

import json
from typing import Any

from services.briefing_db import _get_pool


async def list_open(limit: int = 200) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM user_todos
             WHERE status = 'open'
             ORDER BY
               CASE prioridade
                 WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 END,
               criado_em DESC
             LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def list_recent_done(hours: int = 48, limit: int = 30) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM user_todos
             WHERE status IN ('done', 'archived')
               AND completado_em IS NOT NULL
               AND completado_em >= NOW() - ($1 || ' hours')::interval
             ORDER BY completado_em DESC
             LIMIT $2
            """,
            str(hours),
            limit,
        )
        return [dict(r) for r in rows]


async def create(
    *, titulo: str, detalhe: str | None = None, prioridade: str = "P2",
    empresa: str | None = None, metadata: dict | None = None,
) -> str:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_todos (titulo, detalhe, prioridade, empresa, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING id::text
            """,
            titulo,
            detalhe,
            prioridade,
            empresa,
            json.dumps(metadata) if metadata else None,
        )
        return row["id"] if row else ""


async def complete(todo_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE user_todos
               SET status = 'done', completado_em = NOW()
             WHERE id = $1::uuid AND status = 'open'
            """,
            todo_id,
        )
        return res.endswith(" 1")


async def reopen(todo_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE user_todos
               SET status = 'open', completado_em = NULL
             WHERE id = $1::uuid AND status IN ('done', 'archived')
            """,
            todo_id,
        )
        return res.endswith(" 1")


async def delete(todo_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM user_todos WHERE id = $1::uuid",
            todo_id,
        )
        return res.endswith(" 1")


async def update(
    todo_id: str, *, titulo: str | None = None, detalhe: str | None = None,
    prioridade: str | None = None, empresa: str | None = None,
) -> bool:
    sets: list[str] = []
    args: list[Any] = []
    i = 1
    if titulo is not None:
        sets.append(f"titulo = ${i}"); args.append(titulo); i += 1
    if detalhe is not None:
        sets.append(f"detalhe = ${i}"); args.append(detalhe); i += 1
    if prioridade is not None:
        sets.append(f"prioridade = ${i}"); args.append(prioridade); i += 1
    if empresa is not None:
        sets.append(f"empresa = ${i}"); args.append(empresa); i += 1
    if not sets:
        return False
    args.append(todo_id)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            f"UPDATE user_todos SET {', '.join(sets)} WHERE id = ${i}::uuid",
            *args,
        )
        return res.endswith(" 1")


async def stats() -> dict[str, int]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prioridade, COUNT(*) AS n FROM user_todos WHERE status='open' GROUP BY prioridade"
        )
        out = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        for r in rows:
            out[r["prioridade"]] = r["n"]
        out["total"] = sum(out.values())
        return out
