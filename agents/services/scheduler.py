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


# --- 07-08-2026: traducao do dia da semana ---------------------------------
# cron classico: 0=domingo ... 6=sabado (e 7 tambem e domingo)
# APScheduler:   0=segunda ... 6=domingo
# Sem traduzir, tudo corria um dia depois do que estava escrito.
_DIAS_CRON = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_ORDEM_APS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _traduzir_dow(campo: str) -> str:
    """Converte o dia da semana do cron classico para nomes.

    Devolve o campo intacto se ja vier em nomes, ou se for '*'. Expande
    intervalos para listas explicitas: 0-4 em cron e domingo a quinta, que
    na numeracao do APScheduler seria 6-3, um intervalo invertido que ele
    recusa. Com a lista o problema nao existe.
    """
    campo = (campo or "*").strip()
    if campo in ("*", "?", ""):
        return "*"
    if any(c.isalpha() for c in campo):
        return campo  # ja vem em nomes; nao se mexe

    numeros: set[int] = set()
    for parte in campo.split(","):
        parte = parte.strip()
        passo = 1
        if "/" in parte:
            parte, p = parte.split("/", 1)
            passo = max(1, int(p))
        if parte in ("*", "?"):
            ini, fim = 0, 6
        elif "-" in parte:
            a, b = parte.split("-", 1)
            ini, fim = int(a) % 7, int(b) % 7
        else:
            ini = fim = int(parte) % 7
        pos, i = 0, ini
        while True:
            if pos % passo == 0:
                numeros.add(i)
            if i == fim or pos > 7:
                break
            i = (i + 1) % 7
            pos += 1

    nomes = {_DIAS_CRON[n] for n in numeros}
    return ",".join(d for d in _ORDEM_APS if d in nomes) or "*"


def _parse_cron(expr: str, timezone: str) -> CronTrigger | None:
    """Converte 'min hour dom mon dow' em CronTrigger APScheduler."""
    if not expr:
        return None
    parts = expr.split()
    if len(parts) != 5:
        log.warning("cron expression invalida: %r", expr)
        return None
    minute, hour, day, month, day_of_week = parts
    dow = _traduzir_dow(day_of_week)
    if dow != day_of_week:
        log.info("cron dow traduzido: %r -> %r (expr %r)", day_of_week, dow, expr)
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
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
