"""Worker: marco-health-check-all | Marco | Tech Lead | Sprint 6

Cron: de 15 em 15 minutos, todos os dias.

Verifica saude dos componentes externos ao agents-api (NAO verifica o proprio
agents-api: isso e tarefa do Uptime Kuma de fora). Componentes verificados:
  1. Postgres responsivo (SELECT 1 via pool existente).
  2. Redis responsivo (PING).
  3. Disco / e /var/lib/docker < 90%.
  4. Memoria disponivel > 100 MB.
  5. APScheduler com jobs registados (scheduler.get_jobs() nao vazio).

Comportamento idempotente:
  - Se componente FAILS: emit card P0 com chave estavel "health_<comp>_down".
  - Se componente OK: emit card P3 silencioso de overwrite (mantem chave, baixa
    urgencia, marca como resolvido na DB se status=open).
  - Card-resumo horario "health_resumo_horario" P3 (so quando minuto < 15).

Conforme briefing Marco Sprint 6: NAO verificamos agents-api/health para
evitar auto-referencia (deadlock). Uptime Kuma de fora trata disso.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone

import asyncpg
import structlog

from services.briefing_db import mark_done_by_chave
from services.briefing_emit import emit_briefing

log = structlog.get_logger()


WORKER_NAME = "marco-health-check-all"

POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://omnai_redis:6379/0")

DISK_PATHS = ["/", "/var/lib/docker"]
DISK_THRESHOLD_PCT = 90
MEM_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MB


async def _check_postgres() -> tuple[bool, str]:
    if not POSTGRES_DSN:
        return False, "POSTGRES_DSN ausente"
    conn = None
    try:
        conn = await asyncpg.connect(POSTGRES_DSN, timeout=5)
        val = await conn.fetchval("SELECT 1")
        if val != 1:
            return False, f"select 1 retornou {val!r}"
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _check_redis() -> tuple[bool, str]:
    try:
        import redis.asyncio as aredis  # type: ignore
    except ImportError:
        return False, "biblioteca redis nao instalada"

    client = None
    try:
        client = aredis.from_url(REDIS_URL, socket_timeout=5)
        pong = await client.ping()
        if not pong:
            return False, "PING retornou falsy"
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def _check_disk(path: str) -> tuple[bool, str, dict]:
    try:
        usage = shutil.disk_usage(path)
        used_pct = (usage.used / usage.total) * 100 if usage.total else 0
        meta = {"path": path, "used_pct": round(used_pct, 1), "free_gb": round(usage.free / 1024**3, 2)}
        if used_pct >= DISK_THRESHOLD_PCT:
            return False, f"disco {path} a {used_pct:.1f}% (>{DISK_THRESHOLD_PCT}%)", meta
        return True, "ok", meta
    except FileNotFoundError:
        return True, f"{path} nao existe (skip)", {"path": path, "skip": True}
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {"path": path}


def _check_memory() -> tuple[bool, str, dict]:
    """Le /proc/meminfo. Retorna ok se MemAvailable > 100 MB."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            content = f.read()
        avail_kb = 0
        for line in content.splitlines():
            if line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
                break
        avail_bytes = avail_kb * 1024
        meta = {"mem_available_mb": avail_kb // 1024}
        if avail_bytes < MEM_THRESHOLD_BYTES:
            return False, f"memoria disponivel {avail_kb // 1024} MB (<100 MB)", meta
        return True, "ok", meta
    except FileNotFoundError:
        return True, "/proc/meminfo nao existe (skip)", {"skip": True}
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def _check_scheduler_jobs() -> tuple[bool, str, dict]:
    """Verifica que o APScheduler tem jobs registados."""
    try:
        from services import scheduler as native_scheduler
        jobs = native_scheduler.get_jobs()
        meta = {"job_count": len(jobs)}
        if not jobs:
            return False, "scheduler sem jobs registados", meta
        return True, f"{len(jobs)} jobs", meta
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


async def _emit_or_resolve(component: str, ok: bool, msg: str, meta: dict) -> None:
    """Helper: se ko -> P0 card. Se ok -> resolve card existente com mesma chave."""
    chave_parts = ("component", component)
    if not ok:
        await emit_briefing(
            tipo=f"health_{component}_down",
            titulo=f"Health DOWN: {component}",
            detalhe=f"Check falhou: {msg}",
            urgencia="P0",
            empresa="OMNAI",
            chave_parts=chave_parts,
            metadata={"component": component, "msg": msg[:500], **meta},
            worker_name=WORKER_NAME,
        )
        return

    # Resolve. Reconstroi chave para mark_done_by_chave.
    from services.briefing_db import make_chave
    chave = make_chave(f"health_{component}_down", *chave_parts)
    try:
        await mark_done_by_chave(chave)
    except Exception:
        # Se nao existia, ignora.
        pass


async def run() -> dict:
    started_at = datetime.now(timezone.utc)

    # Postgres
    pg_ok, pg_msg = await _check_postgres()
    await _emit_or_resolve("postgres", pg_ok, pg_msg, {})

    # Redis
    redis_ok, redis_msg = await _check_redis()
    await _emit_or_resolve("redis", redis_ok, redis_msg, {})

    # Disco
    disk_results: dict[str, dict] = {}
    disk_all_ok = True
    for path in DISK_PATHS:
        ok, msg, meta = _check_disk(path)
        component = f"disk_{path.replace('/', '_').strip('_') or 'root'}"
        disk_results[component] = {"ok": ok, "msg": msg, **meta}
        if not ok:
            disk_all_ok = False
        await _emit_or_resolve(component, ok, msg, meta)

    # Memoria
    mem_ok, mem_msg, mem_meta = _check_memory()
    await _emit_or_resolve("memory", mem_ok, mem_msg, mem_meta)

    # Scheduler jobs
    sched_ok, sched_msg, sched_meta = _check_scheduler_jobs()
    await _emit_or_resolve("scheduler", sched_ok, sched_msg, sched_meta)

    all_ok = pg_ok and redis_ok and disk_all_ok and mem_ok and sched_ok

    # Card-resumo horario silencioso, apenas se all_ok e estamos no minuto < 15
    # (so 1 vez por hora). Idempotente via chave horaria.
    if all_ok and started_at.minute < 15:
        hora_iso = started_at.strftime("%Y%m%d-%H")
        await emit_briefing(
            tipo="health_resumo_horario",
            titulo=f"Health OK {started_at.strftime('%H:00')} | todos os componentes",
            detalhe=(
                f"Postgres ok, Redis ok, "
                f"Disk {DISK_PATHS} <{DISK_THRESHOLD_PCT}%, "
                f"Mem {mem_meta.get('mem_available_mb', '?')} MB, "
                f"Scheduler {sched_meta.get('job_count', 0)} jobs"
            ),
            urgencia="P3",
            empresa="OMNAI",
            chave_parts=("hourly", hora_iso),
            metadata={"hora": hora_iso, "checks_ok": True},
            worker_name=WORKER_NAME,
        )

    out = {
        "status": "ok" if all_ok else "warn",
        "all_ok": all_ok,
        "postgres": {"ok": pg_ok, "msg": pg_msg},
        "redis": {"ok": redis_ok, "msg": redis_msg},
        "disk": disk_results,
        "memory": {"ok": mem_ok, "msg": mem_msg, **mem_meta},
        "scheduler": {"ok": sched_ok, "msg": sched_msg, **sched_meta},
    }
    log.info("marco-health-check-all", **out)
    return out
