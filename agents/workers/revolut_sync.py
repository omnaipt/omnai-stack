"""Worker revolut-sync: importa movimentos Revolut e reconcilia com faturas. 08-09-2026.

Diario. Le os ultimos 45 dias (idempotente: upsert por leg_id), corre o
matcher, e entre os dias 1 e 9 emite um cartao P1 com as despesas do mes
anterior sem fatura, para o David tratar antes de a contabilidade perguntar.
Sem /secrets/revolut.json devolve status 'skipped' e nao emite nada.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import structlog

from services import movimentos_db, revolut

log = structlog.get_logger()
WORKER = "revolut-sync"


async def sync(dias: int = 45) -> dict:
    accs = await asyncio.to_thread(revolut.list_accounts)
    contas = {a["id"]: a.get("name") or a.get("currency") for a in accs}
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    txs = await asyncio.to_thread(revolut.list_transactions, desde)
    linhas = []
    for tx in txs:
        if tx.get("state") in ("declined", "failed", "reverted"):
            continue
        linhas.extend(movimentos_db.legs_para_linhas(tx, contas))
    n = await movimentos_db.upsert_linhas(linhas)
    rec = await movimentos_db.reconciliar(desde=(desde - timedelta(days=15)).date())
    return {"contas": len(contas), "transaccoes": len(txs), "linhas": n, **rec}


async def _emitir_sem_fatura() -> int:
    hoje = date.today()
    if hoje.day > 9:
        return 0
    primeiro = hoje.replace(day=1)
    mes_ant = (primeiro - timedelta(days=1)).strftime("%Y-%m")
    itens = await movimentos_db.listar(mes=mes_ant, categoria="despesa",
                                       match_estado="sem_fatura", limit=100)
    if not itens:
        return 0
    try:
        from services.briefing_emit import emit_briefing
    except Exception:
        return 0
    linhas = "\n".join(
        f"- {i['data']:%d/%m} {i['contraparte'] or i['descricao'] or '?'} "
        f"{abs(float(i['valor'])):.2f} {i['moeda']}" for i in itens[:25])
    await emit_briefing(
        tipo="fecho_sem_fatura",
        titulo=f"Fecho {mes_ant}: {len(itens)} despesa(s) Revolut sem fatura",
        urgencia="P1", empresa="OMNAI",
        chave_parts=(mes_ant,),
        detalhe=linhas,
        metadata={"mes": mes_ant, "n": len(itens)},
        worker_name=WORKER,
    )
    return len(itens)


async def run() -> dict:
    if not revolut.configured():
        return {"status": "skipped", "reason": "revolut.json sem client_id/refresh_token"}
    try:
        stats = await sync()
    except Exception as exc:
        log.exception("revolut_sync.fail")
        return {"status": "degraded", "error": f"{type(exc).__name__}: {exc}"}
    emitidos = await _emitir_sem_fatura()
    return {"status": "ok", **stats, "cartao_sem_fatura": emitidos}
