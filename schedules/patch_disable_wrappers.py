"""
patch_disable_wrappers.py

Script idempotente que abre /opt/omnai-stack/schedules/schedules.json e
define enabled=false para os wrappers ciclo-fecho-dia1, ciclo-fecho-dia3 e
ciclo-fecho-dia9. Estes wrappers foram descontinuados em Sprint 4 (substituidos
pelo novo ciclo de fecho consolidado).

Uso:
    python patch_disable_wrappers.py                # aplica
    python patch_disable_wrappers.py --dry-run      # so mostra diff
    python patch_disable_wrappers.py --path X.json  # alvo custom

Saida em stdout em formato curto. Exit code 0 sempre que conseguir ler/escrever;
exit 2 se ficheiro nao existir; exit 3 se JSON invalido.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("patch_disable_wrappers")

DEFAULT_PATH = Path("/opt/omnai-stack/schedules/schedules.json")
TASK_IDS_TO_DISABLE: set[str] = {
    "ciclo-fecho-dia1",
    "ciclo-fecho-dia3",
    "ciclo-fecho-dia9",
}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Disable ciclo-fecho-* wrappers.")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_PATH,
        help=f"Path para schedules.json (default {DEFAULT_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nao escreve, so mostra alteracoes.",
    )
    return parser.parse_args(argv)


def _load(path: Path):
    if not path.exists():
        log.error("schedules.json nao existe em %s", path)
        sys.exit(2)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        log.error("JSON invalido em %s: %s", path, exc)
        sys.exit(3)


def _patch(payload):
    """
    Aceita tres formatos:
        a) [{"taskId": ..., "enabled": ...}, ...]   (formato real OMNAI VPS)
        b) {"tasks": [{"id": ..., "enabled": ...}, ...]}
        c) {"<id>": {"enabled": ..., ...}, ...}
    Devolve payload patched e lista de ids alterados.
    """
    changed: list[str] = []

    # Formato a: lista no top-level (real do VPS OMNAI).
    if isinstance(payload, list):
        for task in payload:
            if not isinstance(task, dict):
                continue
            tid = task.get("taskId") or task.get("id") or task.get("name")
            if tid in TASK_IDS_TO_DISABLE and task.get("enabled") is not False:
                task["enabled"] = False
                changed.append(str(tid))
        return payload, changed

    # Formato b: dict com chave "tasks".
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        for task in payload["tasks"]:
            if not isinstance(task, dict):
                continue
            tid = task.get("id") or task.get("taskId") or task.get("name")
            if tid in TASK_IDS_TO_DISABLE and task.get("enabled") is not False:
                task["enabled"] = False
                changed.append(str(tid))
        return payload, changed

    # Formato c: dict-of-tasks.
    if isinstance(payload, dict):
        for tid, task in payload.items():
            if tid in TASK_IDS_TO_DISABLE and isinstance(task, dict):
                if task.get("enabled") is not False:
                    task["enabled"] = False
                    changed.append(tid)
        return payload, changed

    log.error("formato de schedules.json nao reconhecido: %s", type(payload).__name__)
    sys.exit(3)


def _backup(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)
    log.info("backup criado em %s", backup)
    return backup


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    path: Path = args.path

    payload = _load(path)
    patched, changed = _patch(payload)

    if not changed:
        log.info("nada a alterar (todos os wrappers ja estao disabled ou ausentes)")
        return 0

    log.info("a desactivar %d wrappers: %s", len(changed), ", ".join(sorted(changed)))

    if args.dry_run:
        log.info("--dry-run, nao escrevo")
        print(json.dumps({"changed": changed, "dry_run": True}, indent=2))
        return 0

    _backup(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(patched, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)

    log.info("schedules.json actualizado em %s", path)
    print(json.dumps({"changed": changed, "dry_run": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
