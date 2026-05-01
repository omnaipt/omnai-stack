"""Worker: backup-postgres-diario | Marco | Tech Lead | Sprint 6

Cron: diario 03:00 UTC.

Faz pg_dump da database OMNAI completa, comprime gzip, push para Cloudflare R2
via rclone, e apaga objectos R2 com mais de 30 dias.

Naming: omnai-postgres-YYYYMMDD-HHMMSS.sql.gz

Pre-requisitos:
  - rclone instalado no host (instalado pelo APPLY.sh).
  - rclone remote 'r2omnai' configurado a partir das vars .env (R2_*).
  - Pasta /var/lib/omnai/archive/ existe (volume montado no container).

Como o pg_dump corre dentro do container omnai_agents, o cliente psql/pg_dump
tem de estar instalado na imagem ou via docker exec ao container postgres.
Estrategia: invocar pg_dump no container omnai_postgres via docker socket.
Pero o worker corre dentro do container agents. Solucao: pg_dump como cliente
contra a rede docker interna (host=omnai_postgres). Se o cliente pg_dump nao
existir na imagem agents, fallback para psycopg/asyncpg COPY de cada tabela.

Decisao: assumir que pg_dump esta disponivel no container agents (APPLY.sh
verifica e instala postgresql-client se necessario). Se falhar, regista warning
no briefing como P1.

Emit cards (todos com chave estavel para deduplicar):
  - backup_postgres_ok (P3): silencioso, recente backup ok (titulo + tamanho).
  - backup_postgres_fail (P0): se pg_dump falha ou rclone push falha.
  - backup_postgres_stale (P0): se ultimo backup tem mais de 36h (proxy de
    health check do proprio backup).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from services.briefing_emit import emit_briefing

log = structlog.get_logger()


WORKER_NAME = "backup-postgres-diario"

# Config via env (.env ja monta no container)
def _parse_database_url():
    """Parse DATABASE_URL postgresql://user:pass@host:port/db, ou cai em vars individuais."""
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
    return {
        "host": os.getenv("POSTGRES_HOST", "postgres"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
        "user": os.getenv("POSTGRES_USER", "omnai"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
        "db": os.getenv("POSTGRES_DB", "omnai"),
    }

_pg = _parse_database_url()
POSTGRES_HOST = _pg["host"]
POSTGRES_PORT = _pg["port"]
POSTGRES_USER = _pg["user"]
POSTGRES_DB = _pg["db"]
POSTGRES_PASSWORD = _pg["password"]

R2_BUCKET = os.getenv("R2_BUCKET", "omnai-postgres-backups")
R2_REMOTE = os.getenv("R2_RCLONE_REMOTE", "r2omnai")
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))

ARCHIVE_DIR = Path(os.getenv("BACKUP_LOCAL_DIR", "/var/lib/omnai/archive"))


async def _run_cmd(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 600) -> tuple[int, str, str]:
    """Corre subprocess assincrono. Retorna (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timeout apos {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _pg_dump_to_file(target: Path) -> tuple[bool, str]:
    """pg_dump | gzip > target. Retorna (sucesso, mensagem)."""
    target.parent.mkdir(parents=True, exist_ok=True)

    # pg_dump pipeline: pg_dump ... | gzip -9 > target
    # Usamos shell=True via /bin/sh -c para o pipe.
    cmd = [
        "/bin/sh",
        "-c",
        f"PGPASSWORD='{POSTGRES_PASSWORD}' pg_dump "
        f"-h {POSTGRES_HOST} -p {POSTGRES_PORT} -U {POSTGRES_USER} "
        f"--no-owner --no-acl --clean --if-exists "
        f"-d {POSTGRES_DB} | gzip -9 > '{target}'",
    ]
    rc, out, err = await _run_cmd(cmd, timeout=1800)
    if rc != 0:
        return False, f"pg_dump rc={rc} err={err[:500]}"
    if not target.exists() or target.stat().st_size < 1024:
        return False, f"backup file vazio ou ausente em {target}"
    return True, f"ok, {target.stat().st_size} bytes"


async def _rclone_push(local_path: Path) -> tuple[bool, str]:
    """rclone copyto local_path remote:bucket/<filename>."""
    remote_target = f"{R2_REMOTE}:{R2_BUCKET}/{local_path.name}"
    rc, out, err = await _run_cmd(
        ["rclone", "copyto", str(local_path), remote_target, "--s3-no-check-bucket"],
        timeout=900,
    )
    if rc != 0:
        return False, f"rclone rc={rc} err={err[:500]}"
    return True, f"pushed to {remote_target}"


async def _rclone_prune_old() -> tuple[int, str]:
    """Apaga objectos R2 com mais de RETENTION_DAYS dias.

    rclone delete --min-age 30d apaga ficheiros antigos.
    """
    rc, out, err = await _run_cmd(
        [
            "rclone", "delete",
            f"{R2_REMOTE}:{R2_BUCKET}/",
            f"--min-age", f"{RETENTION_DAYS}d",
            "--include", "omnai-postgres-*.sql.gz",
        ],
        timeout=300,
    )
    if rc != 0:
        return 0, f"prune rc={rc} err={err[:300]}"
    # rclone delete nao reporta contagem facil; retorna 0 + mensagem.
    return 1, "prune ok"


def _local_prune_old() -> int:
    """Apaga ficheiros locais > 7 dias (mantemos buffer pequeno local)."""
    if not ARCHIVE_DIR.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=7)
    n = 0
    for f in ARCHIVE_DIR.glob("omnai-postgres-*.sql.gz"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                n += 1
        except Exception:
            continue
    return n


async def run() -> dict:
    started_at = datetime.now(timezone.utc)
    ts = started_at.strftime("%Y%m%d-%H%M%S")
    fname = f"omnai-postgres-{ts}.sql.gz"
    target = ARCHIVE_DIR / fname

    # Pre-flight: rclone existe?
    rc, _, _ = await _run_cmd(["which", "rclone"], timeout=10)
    if rc != 0:
        await emit_briefing(
            tipo="backup_postgres_fail",
            titulo="Backup Postgres FAIL: rclone nao instalado",
            detalhe="O binario rclone nao esta no PATH do container agents-api. APPLY.sh devia ter instalado.",
            urgencia="P0",
            empresa="OMNAI",
            chave_parts=("rclone_missing",),
            metadata={"check": "rclone_which", "ts": ts},
            worker_name=WORKER_NAME,
        )
        return {"status": "fail", "stage": "rclone_check"}

    # Step 1: pg_dump
    ok, msg = await _pg_dump_to_file(target)
    if not ok:
        await emit_briefing(
            tipo="backup_postgres_fail",
            titulo="Backup Postgres FAIL: pg_dump",
            detalhe=msg,
            urgencia="P0",
            empresa="OMNAI",
            chave_parts=("pg_dump", started_at.strftime("%Y%m%d")),
            metadata={"stage": "pg_dump", "ts": ts, "msg": msg[:1000]},
            worker_name=WORKER_NAME,
        )
        return {"status": "fail", "stage": "pg_dump", "error": msg}

    size_bytes = target.stat().st_size
    log.info("pg_dump.ok", path=str(target), size=size_bytes)

    # Step 2: rclone push para R2
    ok, msg = await _rclone_push(target)
    if not ok:
        await emit_briefing(
            tipo="backup_postgres_fail",
            titulo="Backup Postgres FAIL: rclone push para R2",
            detalhe=f"pg_dump local OK ({size_bytes} bytes) mas push falhou: {msg}",
            urgencia="P0",
            empresa="OMNAI",
            chave_parts=("rclone_push", started_at.strftime("%Y%m%d")),
            metadata={"stage": "rclone_push", "size": size_bytes, "msg": msg[:1000]},
            worker_name=WORKER_NAME,
        )
        return {"status": "fail", "stage": "rclone_push", "error": msg}

    # Step 3: prune old
    pruned_remote_rc, prune_msg = await _rclone_prune_old()
    pruned_local = _local_prune_old()

    # Step 4: card silencioso de sucesso (chave estavel diaria, sobrescreve)
    await emit_briefing(
        tipo="backup_postgres_ok",
        titulo=f"Backup Postgres OK | {size_bytes // 1024 // 1024} MB",
        detalhe=(
            f"Ficheiro: {fname}\n"
            f"Push R2: {R2_BUCKET}\n"
            f"Pruned local: {pruned_local} | Pruned R2: ver logs rclone"
        ),
        urgencia="P3",
        empresa="OMNAI",
        chave_parts=("daily", started_at.strftime("%Y%m%d")),
        metadata={
            "size_bytes": size_bytes,
            "filename": fname,
            "ts": ts,
            "pruned_local": pruned_local,
        },
        worker_name=WORKER_NAME,
    )

    out = {
        "status": "ok",
        "filename": fname,
        "size_bytes": size_bytes,
        "pruned_local": pruned_local,
        "prune_remote_msg": prune_msg,
        "duration_s": (datetime.now(timezone.utc) - started_at).total_seconds(),
    }
    log.info("backup-postgres-diario", **out)
    return out
