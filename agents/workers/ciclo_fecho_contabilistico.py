"""ciclo-fecho-contabilistico v1.0 - worker consolidado Ana (CFO).

Substitui 3 workers antigos (alerta-fecho-contabilistico-dia1,
alerta-extractos-bancarios-dia3, alerta-deadline-contabilidade-dia9) por
1 modulo com 3 entrypoints, um por etapa do ciclo mensal de fecho.

V1 delega aos workers antigos. V2 (futuro) fundira codigo comum (template
de email, estado mensal, etc).

Task IDs / crons:
  * ciclo-fecho-dia1     - dia 1  09:00 (abertura)
  * ciclo-fecho-dia3     - dia 3  09:00 (lembrete extractos bancarios)
  * ciclo-fecho-dia9     - dia 9  09:00 (deadline iminente)

Output: dict com 'step' + resultado do sub-worker.
"""
from __future__ import annotations

from typing import Any

import structlog

from workers import (
    alerta_deadline_contabilidade_dia9,
    alerta_extractos_bancarios_dia3,
    alerta_fecho_contabilistico_dia1,
)

log = structlog.get_logger()


async def run_open() -> dict[str, Any]:
    """Dia 1 - abre o ciclo mensal."""
    log.info("ciclo_fecho.step.start", step="open")
    res = await alerta_fecho_contabilistico_dia1.run()
    log.info("ciclo_fecho.step.done", step="open", status=res.get("status"))
    return {"status": "ok", "step": "open", **res}


async def run_extractos() -> dict[str, Any]:
    """Dia 3 - lembrete extractos bancarios."""
    log.info("ciclo_fecho.step.start", step="extractos")
    res = await alerta_extractos_bancarios_dia3.run()
    log.info("ciclo_fecho.step.done", step="extractos", status=res.get("status"))
    return {"status": "ok", "step": "extractos", **res}


async def run_deadline() -> dict[str, Any]:
    """Dia 9 - deadline contabilidade (pacote deve ir amanha)."""
    log.info("ciclo_fecho.step.start", step="deadline")
    res = await alerta_deadline_contabilidade_dia9.run()
    log.info("ciclo_fecho.step.done", step="deadline", status=res.get("status"))
    return {"status": "ok", "step": "deadline", **res}


# Default alias - chama o step correspondente ao dia do mes
async def run() -> dict[str, Any]:
    from datetime import date
    today = date.today().day
    if today <= 2:
        return await run_open()
    if today <= 5:
        return await run_extractos()
    return await run_deadline()
