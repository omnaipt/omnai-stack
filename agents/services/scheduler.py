"""Scheduler nativo do agents-api.

Le schedules.json no startup, cria cron jobs in-process com APScheduler.
Cada job chama a funcao run() do worker correspondente, sem HTTP externo.

Substitui o papel do n8n para os nossos crons (Despesas Bot v2/v3 ficam fora).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

SCHEDULES_FILE = Path(os.getenv("SCHEDULES_FILE", "/app/schedules/schedules.json"))


_scheduler: AsyncIOScheduler | None = None


def _parse_cron(expr: str, timezone: str) -> CronTrigger | None:
    """Converte 'min hour dom mon dow' em CronTrigger APScheduler."""
    if not expr:
        return None
    parts = expr.split()
    if len(parts) != 5:
        log.warning("cron expression invalida: %r", expr)
        return None
    minute, hour, day, month, day_of_week = parts
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=timezone or "Europe/Lisbon",
        )
    except Exception as exc:
        log.warning("cron parse FAIL expr=%r err=%s", expr, exc)
        return None


def _make_job(task_id: str, runner: Callable[[], Awaitable[dict]]) -> Callable[[], Awaitable[None]]:
    """Wrapper que loga inicio/fim e captura excepcoes."""
    async def _job() -> None:
        log.info("scheduler.job.start", extra={"task_id": task_id})
        try:
            result = await runner()
            log.info(
                "scheduler.job.done",
                extra={"task_id": task_id, "status": (result or {}).get("status")},
            )
        except Exception as exc:
            log.exception("scheduler.job.error task_id=%s err=%s", task_id, exc)
    return _job


def start(workers_registry: dict[str, Callable[[], Awaitable[dict]]]) -> AsyncIOScheduler:
    """Cria scheduler, le schedules.json, regista jobs activos.

    `workers_registry` e o dicionario WORKERS do main.py, mapeando task_id -> async run().
    Apenas tasks com `enabled=true` e que tenham worker real vao para o scheduler.
    """
    global _scheduler

    if not SCHEDULES_FILE.exists():
        log.warning("scheduler: %s nao existe, sem jobs agendados", SCHEDULES_FILE)
        _scheduler = AsyncIOScheduler(timezone="Europe/Lisbon")
        _scheduler.start()
        return _scheduler

    schedules = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    sched = AsyncIOScheduler(timezone="Europe/Lisbon")

    registered = 0
    skipped_disabled = 0
    skipped_no_worker = 0

    for entry in schedules:
        task_id = entry.get("taskId")
        if not task_id:
            continue
        if not entry.get("enabled", True):
            skipped_disabled += 1
            continue

        runner = workers_registry.get(task_id)
        if runner is None:
            skipped_no_worker += 1
            continue

        trigger = _parse_cron(
            entry.get("cronExpression", ""),
            entry.get("timezone", "Europe/Lisbon"),
        )
        if trigger is None:
            continue

        sched.add_job(
            _make_job(task_id, runner),
            trigger=trigger,
            id=task_id,
            name=entry.get("description", task_id)[:60],
            replace_existing=True,
            misfire_grace_time=60 * 30,  # 30 min de tolerancia se container estava down
            coalesce=True,
        )
        registered += 1

    sched.start()
    log.info(
        "scheduler.started registered=%d skipped_disabled=%d skipped_no_worker=%d",
        registered, skipped_disabled, skipped_no_worker,
    )

    _scheduler = sched
    return sched


def stop() -> None:
    """Encerra scheduler (para no shutdown do FastAPI)."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
            log.info("scheduler.stopped")
        except Exception as exc:
            log.warning("scheduler.stop FAIL err=%s", exc)
        _scheduler = None


def get_jobs() -> list[dict]:
    """Devolve estado actual dos jobs (para debug ou /scheduler/status endpoint)."""
    if _scheduler is None:
        return []
    out: list[dict] = []
    for job in _scheduler.get_jobs():
        next_run = job.next_run_time.isoformat() if job.next_run_time else None
        out.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run,
            "trigger": str(job.trigger),
        })
    return out
