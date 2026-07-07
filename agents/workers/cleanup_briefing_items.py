"""Worker: cleanup-briefing-items | Marco | Tech Lead | Sprint 6

Cron: mensal, dia 1 as 04:00 UTC.

Antes de DELETE: pg_dump da tabela briefing_items para ficheiro local em
/var/lib/omnai/archive/briefing_items_YYYYMM.sql.gz, depois rclone push para
R2 mesmo bucket dos backups Postgres.

DELETE: WHERE status IN ('done', 'dismissed') AND resolvido_em < NOW() - INTERVAL '90 days'.

Emit cards:
  - cleanup_briefing_ok (P3): card silencioso mensal com contagem de linhas
    apagadas e ficheiro arquivado.
  - cleanup_briefing_fail (P0): se dump ou DELETE falham.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import structlog

from services.briefing_emit import emit_briefing

log = structlog.get_logger()


WORKER_NAME = "cleanup-briefing-items"

def _parse_database_url():
    import urllib.parse
    url = os.getenv("DATABASE_URL", "")
    if url:
        u = urllib.parse.urlparse(url)
        return {
            "host": u.hostname or "postgres",
            "port": str(u.port or 5432),
            "user": u.username or "omnai",
            "password": urllib.parse.unquote(u.password or ""),
            "db": (u.path or "/omnai").lstrip("/"),
        }
    return {"host":"postgres","port":"5432","user":"omnai","password":"","db":"omnai"}

_pg = _parse_database_url()
POSTGRES_HOST = _pg["host"]
POSTGRES_PORT = _pg["port"]
POSTGRES_USER = _pg["user"]
POSTGRES_DB = _pg["db"]
POSTGRES_PASSWORD = _pg["password"]
POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")

R2_BUCKET = os.getenv("R2_BUCKET", "omnai-postgres-backups")
R2_REMOTE = os.getenv("R2_RCLONE_REMOTE", "r2omnai")
ARCHIVE_DIR = Path(os.getenv("BACKUP_LOCAL_DIR", "/var/lib/omnai/archive"))
RETENTION_DAYS = int(os.getenv("CLEANUP_BRIEFING_DAYS", "90"))


async def _run_cmd(cmd: list[str], timeout: int = 600) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timeout apos {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _dump_briefing_items_table(target: Path) -> tuple[bool, str]:
    """pg_dump --table=briefing_items | gzip > target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "/bin/sh", "-c",
        f"PGPASSWORD='{POSTGRES_PASSWORD}' pg_dump "
        f"-h {POSTGRES_HOST} -p {POSTGRES_PORT} -U {POSTGRES_USER} "
        f"--no-owner --no-acl --table=briefing_items --data-only "
        f"-d {POSTGRES_DB} | gzip -9 > '{target}'",
    ]
    rc, out, err = await _run_cmd(cmd, timeout=600)
    if rc != 0:
        return False, f"pg_dump rc={rc} err={err[:500]}"
    if not target.exists() or target.stat().st_size < 100:
        return False, "ficheiro arquivo vazio ou ausente"
    return True, "ok"


async def _rclone_push(local: Path) -> tuple[bool, str]:
    target = f"{R2_REMOTE}:{R2_BUCKET}/cleanup/{local.name}"
    rc, _, err = await _run_cmd(
        ["rclone", "copyto", str(local), target, "--s3-no-check-bucket"],
        timeout=600,
    )
    if rc != 0:
        return False, f"rclone rc={rc} err={err[:500]}"
    return True, target


async def _delete_old_briefing_items() -> tuple[int, str]:
    if not POSTGRES_DSN:
        return 0, "POSTGRES_DSN ausente"
    try:
        conn = await asyncpg.connect(POSTGRES_DSN, timeout=10)
    except Exception as exc:
        return 0, f"connect FAIL: {exc}"

    try:
        # Conta antes de apagar (telemetria)
        n = await conn.fetchval(
            f"""
            SELECT COUNT(*) FROM briefing_items
             WHERE status IN ('done', 'dismissed')
               AND resolvido_em IS NOT NULL
               AND resolvido_em < NOW() - INTERVAL '{RETENTION_DAYS} days'
            """
        )
        if not n:
            return 0, "nenhum candidato a apagar"
        await conn.execute(
            f"""
            DELETE FROM briefing_items
             WHERE status IN ('done', 'dismissed')
               AND resolvido_em IS NOT NULL
               AND resolvido_em < NOW() - INTERVAL '{RETENTION_DAYS} days'
            """
        )
        return n, f"apagadas {n} linhas"
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def run() -> dict:
    now = datetime.now(timezone.utc)
    yyyymm = now.strftime("%Y%m")
    fname = f"briefing_items_{yyyymm}.sql.gz"
    target = ARCHIVE_DIR / fname

    # Step 1: dump
    ok, msg = await _dump_briefing_items_table(target)
    if not ok:
        await emit_briefing(
            tipo="cleanup_briefing_fail",
            titulo="Cleanup briefing FAIL: pg_dump",
            detalhe=msg,
            urgencia="P0",
            empresa="OMNAI",
            chave_parts=("dump", yyyymm),
            metadata={"stage": "dump", "msg": msg[:500]},
            worker_name=WORKER_NAME,
        )
        return {"status": "fail", "stage": "dump", "error": msg}

    # Step 2: push R2 (best effort)
    push_ok, push_msg = await _rclone_push(target)
    if not push_ok:
        log.warning("cleanup.rclone_push.fail", err=push_msg)
        # Continua: o ficheiro local serve como salvaguarda. Card P1.
        await emit_briefing(
            tipo="cleanup_briefing_partial",
            titulo="Cleanup briefing: dump OK mas push R2 FAIL",
            detalhe=f"Dump local em {target}. Push falhou: {push_msg}",
            urgencia="P1",
            empresa="OMNAI",
            chave_parts=("push_partial", yyyymm),
            metadata={"target": str(target), "push_msg": push_msg[:500]},
            worker_name=WORKER_NAME,
        )

    # Step 3: DELETE
    n_deleted, del_msg = await _delete_old_briefing_items()

    await emit_briefing(
        tipo="cleanup_briefing_ok",
        titulo=f"Cleanup briefing {yyyymm}: {n_deleted} linhas apagadas",
        detalhe=(
            f"Arquivo: {fname} ({target.stat().st_size if target.exists() else 0} bytes)\n"
            f"Push R2: {'OK ' + push_msg if push_ok else 'FAIL'}\n"
            f"Retention: {RETENTION_DAYS}d\n"
            f"Linhas apagadas: {n_deleted} ({del_msg})"
        ),
        urgencia="P3",
        empresa="OMNAI",
        chave_parts=("monthly", yyyymm),
        metadata={
            "yyyymm": yyyymm,
            "deleted": n_deleted,
            "push_ok": push_ok,
            "filename": fname,
        },
        worker_name=WORKER_NAME,
    )

    out = {
        "status": "ok",
        "yyyymm": yyyymm,
        "filename": fname,
        "deleted": n_deleted,
        "push_ok": push_ok,
    }
    log.info("cleanup-briefing-items", **out)
    return out
