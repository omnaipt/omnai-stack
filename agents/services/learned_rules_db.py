"""CRUD para learned_rules (Postgres).

Cache em memoria para o classifier consultar sem hit DB por cada email.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from services.briefing_db import _get_pool

log = logging.getLogger(__name__)


VALID_TYPES = ("sender", "domain", "subject_keyword")
VALID_CLASSES = ("actionable", "invoice", "archive", "delete", "keep")

# Cache em memoria: { rule_type: [(pattern_compiled, forced_class, rule_id), ...] }
_CACHE: dict[str, list[tuple]] = {"sender": [], "domain": [], "subject_keyword": []}


def _compile_pattern(rule_type: str, pattern: str):
    """Compila pattern em regex. sender/domain = matching literal case-insensitive.
    subject_keyword = palavra-chave em qualquer local do subject (word boundary).
    """
    if rule_type == "sender":
        # Match literal do endereco em qualquer parte do from_addr
        return re.compile(re.escape(pattern), re.I)
    if rule_type == "domain":
        # Match @domain em qualquer parte
        pat = pattern.lstrip("@")
        return re.compile(r"@" + re.escape(pat) + r"\b", re.I)
    if rule_type == "subject_keyword":
        return re.compile(r"\b" + re.escape(pattern) + r"\b", re.I)
    return None


async def reload_cache() -> dict[str, int]:
    """Recarrega cache da DB. Devolve contagem por tipo."""
    pool = await _get_pool()
    new_cache: dict[str, list[tuple]] = {"sender": [], "domain": [], "subject_keyword": []}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text AS id, rule_type, pattern, forced_class FROM learned_rules WHERE enabled = TRUE"
        )
        for r in rows:
            rx = _compile_pattern(r["rule_type"], r["pattern"])
            if rx is None:
                continue
            new_cache[r["rule_type"]].append((rx, r["forced_class"], r["id"]))
    _CACHE["sender"] = new_cache["sender"]
    _CACHE["domain"] = new_cache["domain"]
    _CACHE["subject_keyword"] = new_cache["subject_keyword"]
    stats = {k: len(v) for k, v in new_cache.items()}
    log.info("learned_rules.cache_loaded %s", stats)
    return stats


def check(subject: str, from_addr: str) -> tuple[str, str] | None:
    """Devolve (forced_class, rule_id) se match, None caso contrario.

    Ordem: sender (mais especifico) -> domain -> subject_keyword.
    """
    if from_addr:
        for rx, cls, rid in _CACHE["sender"]:
            if rx.search(from_addr):
                return cls, rid
        for rx, cls, rid in _CACHE["domain"]:
            if rx.search(from_addr):
                return cls, rid
    if subject:
        for rx, cls, rid in _CACHE["subject_keyword"]:
            if rx.search(subject):
                return cls, rid
    return None


async def add(
    *, rule_type: str, pattern: str, forced_class: str,
    source: str = "manual", notes: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Insere/upserts uma regra. Devolve id."""
    if rule_type not in VALID_TYPES:
        raise ValueError(f"rule_type invalido: {rule_type}")
    if forced_class not in VALID_CLASSES:
        raise ValueError(f"forced_class invalida: {forced_class}")
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("pattern vazio")

    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO learned_rules (rule_type, pattern, forced_class, source, notes, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (rule_type, pattern) DO UPDATE SET
                forced_class = EXCLUDED.forced_class,
                source = EXCLUDED.source,
                notes = COALESCE(EXCLUDED.notes, learned_rules.notes),
                enabled = TRUE
            RETURNING id::text
            """,
            rule_type, pattern, forced_class, source, notes,
            json.dumps(metadata) if metadata else None,
        )
    await reload_cache()
    return row["id"] if row else ""


async def disable(rule_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE learned_rules SET enabled = FALSE WHERE id = $1::uuid", rule_id,
        )
    await reload_cache()
    return res.endswith(" 1")


async def delete(rule_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM learned_rules WHERE id = $1::uuid", rule_id,
        )
    await reload_cache()
    return res.endswith(" 1")


async def list_all(enabled_only: bool = False, limit: int = 500) -> list[dict]:
    pool = await _get_pool()
    where = "WHERE enabled = TRUE" if enabled_only else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM learned_rules {where} ORDER BY hit_count DESC, criado_em DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]


async def record_hit(rule_id: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE learned_rules SET hit_count = hit_count + 1, last_hit_at = NOW() WHERE id = $1::uuid",
            rule_id,
        )
