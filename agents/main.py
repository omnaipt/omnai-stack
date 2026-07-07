"""OMNAI Agents API v0.3.1 (Sprint 9).

Sprint 9: ao clicar [Resolver] ou [Dispensar] num card de email, arquivar
o email original no Gmail. Snooze NAO arquiva. Novo endpoint
/actions/draft-done apaga draft de Redis e arquiva o email.

Funcionalidade existente preservada na integra. Falha do Gmail archive
nao bloqueia o redirect: apenas log.warning.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from auth import require_token

from workers import (
    briefing_carlos,
    briefing_inbox,
    email_scan,
    verificacao_recibos_sopato,
    alerta_fecho_contabilistico_dia1,
    alerta_extractos_bancarios_dia3,
    alerta_deadline_contabilidade_dia9,
    arquivo_faturas,
    check_deadlines_legais,
    pipeline_review_semanal,
    analise_concorrencia_semanal,
    scan_concursos_publicos,
    inbox_sweep,
    ciclo_fecho_contabilistico,
    learn_from_sapo_trash,
)

log = structlog.get_logger()

SCHEDULES_FILE = Path(os.getenv("SCHEDULES_FILE", "/app/schedules/schedules.json"))
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def load_schedules() -> list[dict[str, Any]]:
    if not SCHEDULES_FILE.exists():
        log.warning("schedules.json nao encontrado", path=str(SCHEDULES_FILE))
        return []
    return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))


SCHEDULES: list[dict[str, Any]] = load_schedules()

AGENTS = {
    "ana":     {"role": "CFO",             "areas": ["financas", "contabilidade"]},
    "marco":   {"role": "Tech Lead",       "areas": ["backend", "infra", "apis"]},
    "sofia":   {"role": "Product Owner",   "areas": ["backlog", "roadmap"]},
    "ze":      {"role": "Design Engineer", "areas": ["ux", "frontend", "mobile"]},
    "rita":    {"role": "Marketing Lead",  "areas": ["conteudo", "seo", "redes"]},
    "tiago":   {"role": "Sales",           "areas": ["crm", "pipeline"]},
    "beatriz": {"role": "Legal",           "areas": ["contratos", "rgpd"]},
    "carlos":  {"role": "Dispatcher",      "areas": ["routing", "briefings"]},
}

WORKERS = {
    "briefing-carlos": briefing_inbox.run,  # v9.2 substitui o worker antigo
    "email-scan": email_scan.run,
    "verificacao-recibos-sopato": verificacao_recibos_sopato.run,
    "alerta-fecho-contabilistico-dia1": alerta_fecho_contabilistico_dia1.run,
    "alerta-extractos-bancarios-dia3": alerta_extractos_bancarios_dia3.run,
    "alerta-deadline-contabilidade-dia9": alerta_deadline_contabilidade_dia9.run,
    "arquivo-faturas": arquivo_faturas.run,
    "check-deadlines-legais": check_deadlines_legais.run,
    "pipeline-review-semanal": pipeline_review_semanal.run,
    "analise-concorrencia-semanal": analise_concorrencia_semanal.run,
    "scan-concursos-publicos": scan_concursos_publicos.run,
    "inbox-sweep-morning": inbox_sweep.run_morning,
    "inbox-sweep-midday": inbox_sweep.run_midday,
    "inbox-sweep-evening": inbox_sweep.run_evening,
    "ciclo-fecho-dia1": ciclo_fecho_contabilistico.run_open,
    "ciclo-fecho-dia3": ciclo_fecho_contabilistico.run_extractos,
    "ciclo-fecho-dia9": ciclo_fecho_contabilistico.run_deadline,
    "learn-from-sapo-trash": learn_from_sapo_trash.run,
}

from contextlib import asynccontextmanager

from services import scheduler as native_scheduler


@asynccontextmanager
async def lifespan(app):
    log.info("lifespan.start scheduler arrancar")
    from services import learned_rules_db as _learned_rules_db
    try:
        await _learned_rules_db.reload_cache()
    except Exception as exc:
        log.warning("learned_rules cache load FAIL", err=str(exc))
    native_scheduler.start(WORKERS)
    yield
    log.info("lifespan.stop scheduler a parar")
    native_scheduler.stop()


app = FastAPI(title="OMNAI Agents API", version="0.3.1", lifespan=lifespan)


class DispatchPayload(BaseModel):
    source: str
    payload: dict[str, Any]


@app.get("/health")
def health() -> dict[str, Any]:
    """Endpoint publico, sem auth, para healthchecks."""
    return {
        "status": "ok",
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "0.3.1",
        "model": ANTHROPIC_MODEL,
        "schedules_loaded": len(SCHEDULES),
        "agents": list(AGENTS.keys()),
        "workers_implemented": sorted(WORKERS.keys()),
        "auth_required": True,
    }


@app.get("/agents", dependencies=[Depends(require_token)])
def list_agents() -> dict[str, Any]:
    return {"agents": AGENTS}


@app.get("/tasks", dependencies=[Depends(require_token)])
def list_tasks() -> dict[str, Any]:
    return {"count": len(SCHEDULES), "tasks": SCHEDULES}


@app.post("/tasks/run/{task_id}", dependencies=[Depends(require_token)])
async def run_task(task_id: str, request: Request) -> dict[str, Any]:
    task = next((t for t in SCHEDULES if t["taskId"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' nao encontrada")

    log.info("task.run.start", task_id=task_id, description=task.get("description"))

    worker = WORKERS.get(task_id)
    if worker:
        try:
            result = await worker()
            log.info("task.run.done", task_id=task_id, result_status=result.get("status"))
            return {
                "status": "completed",
                "task_id": task_id,
                "worker_module": worker.__module__,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            }
        except Exception as exc:
            log.exception("task.run.error", task_id=task_id)
            raise HTTPException(
                status_code=500,
                detail=f"worker error: {type(exc).__name__}: {exc}",
            )

    return {
        "status": "accepted",
        "task_id": task_id,
        "description": task.get("description"),
        "worker": "stub",
        "note": "Sem worker implementado para este task_id ainda.",
    }


@app.post("/dispatch", dependencies=[Depends(require_token)])
def dispatch(payload: DispatchPayload) -> dict[str, Any]:
    log.info("dispatch.received", source=payload.source)
    return {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "routed_to": "carlos",
        "source": payload.source,
    }


# ====== v9.2 actions endpoints + Sprint 9 archive on resolve/dismiss/draft-done ======
from fastapi.responses import HTMLResponse  # noqa: E402

from services import briefing_db, state  # noqa: E402
from services.action_tokens import verify_token  # noqa: E402


# Tipos considerados "email" para fins de arquivamento.
# Defensivo: tambem inferimos por presenca de gmail_message_id em metadata
# (caso futuro tipo seja adicionado sem tocar nesta lista).
EMAIL_TIPOS: set[str] = {
    "email_actionable",
    "email_fatura_pendente",
    "fatura_sem_documento",
}


def _is_email_item(item: dict | None) -> bool:
    if not item:
        return False
    if (item.get("tipo") or "") in EMAIL_TIPOS:
        return True
    md = item.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return bool(md.get("gmail_message_id") and (md.get("gmail_inbox") or md.get("inbox")))


def _gmail_coords(item: dict | None) -> tuple[str | None, str | None]:
    """Extrai (inbox, message_id) do metadata do item, se existir."""
    if not item:
        return None, None
    md = item.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    inbox = md.get("gmail_inbox") or md.get("inbox")
    msg_id = md.get("gmail_message_id")
    return inbox, msg_id


async def _archive_if_email(item_id: str, action: str) -> None:
    """Best-effort archive do email original. Nao bloqueia o redirect."""
    try:
        item = await briefing_db.get_by_id(item_id)
    except Exception as exc:
        log.warning("archive_if_email.get_by_id_fail", item_id=item_id, err=str(exc))
        return
    if not _is_email_item(item):
        return
    inbox, msg_id = _gmail_coords(item)
    if not (inbox and msg_id):
        log.info("archive_if_email.skip", item_id=item_id, reason="metadata_incompleta")
        return
    try:
        from services.gmail_archive import archive_message
        ok, msg = await archive_message(inbox, msg_id)
        if not ok:
            log.warning("archive_failed", item_id=item_id, action=action, err=msg)
        else:
            log.info("archive_ok", item_id=item_id, action=action, inbox=inbox)
    except Exception as exc:
        log.warning("archive_failed.exc", item_id=item_id, action=action, err=str(exc))


def _action_html(emoji: str, titulo: str, sub: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo}</title>
<style>
body{{font-family:system-ui;-webkit-font-smoothing:antialiased;
     display:flex;align-items:center;justify-content:center;
     min-height:100vh;margin:0;background:#fafafa;color:#111}}
.box{{background:#fff;padding:40px;border-radius:12px;
     box-shadow:0 1px 3px rgba(0,0,0,0.08);text-align:center;max-width:420px}}
.emoji{{font-size:48px;margin-bottom:16px}}
h1{{margin:0 0 8px;font-size:22px}}
p{{margin:0;color:#555}}
</style></head>
<body><div class="box">
<div class="emoji">{emoji}</div>
<h1>{titulo}</h1>
<p>{sub}</p>
</div></body></html>"""


# ====== v9.2.1 actions endpoints (auto-refresh + redirect ao Notion) ======
NOTION_BRIEFING_URL = "https://www.notion.so/33e973b9238781078667eadc9128ab27"
MORNING_BRIEFING_URL = NOTION_BRIEFING_URL  # alias semantico


@app.get("/actions/done")
async def action_done(id: str, t: str, d: int = 0):
    from fastapi.responses import RedirectResponse
    from workers import briefing_inbox

    ok, _ = verify_token(id, "done", t)
    if not ok:
        return _action_html("Token invalido", "Esse link expirou ou foi alterado.")

    await briefing_db.mark_done(id)

    # Sprint 9: arquivar email original se aplicavel (best-effort).
    await _archive_if_email(id, "done")

    try:
        await briefing_inbox.run()
    except Exception as exc:
        log.warning("briefing_inbox refresh FAIL", err=str(exc))

    return RedirectResponse(url=NOTION_BRIEFING_URL, status_code=302)


@app.get("/actions/snooze")
async def action_snooze(id: str, t: str, d: int = 7):
    from fastapi.responses import RedirectResponse
    from workers import briefing_inbox

    ok, days = verify_token(id, "snooze", t)
    if not ok:
        return _action_html("Token invalido", "Esse link expirou ou foi alterado.")

    days = max(1, min(60, days or d))
    await briefing_db.snooze(id, days=days)

    # Sprint 9: snooze NAO arquiva. Por design.

    try:
        await briefing_inbox.run()
    except Exception as exc:
        log.warning("briefing_inbox refresh FAIL", err=str(exc))

    return RedirectResponse(url=NOTION_BRIEFING_URL, status_code=302)


@app.get("/actions/dismiss")
async def action_dismiss(id: str, t: str):
    from fastapi.responses import RedirectResponse
    from workers import briefing_inbox

    ok, _ = verify_token(id, "dismiss", t)
    if not ok:
        return _action_html("Token invalido", "")

    await briefing_db.mark_dismissed(id)

    # Sprint 9: arquivar email original se aplicavel (best-effort).
    await _archive_if_email(id, "dismiss")

    try:
        await briefing_inbox.run()
    except Exception as exc:
        log.warning("briefing_inbox refresh FAIL", err=str(exc))

    return RedirectResponse(url=NOTION_BRIEFING_URL, status_code=302)


@app.get("/actions/draft-done")
async def action_draft_done(id: str, t: str):
    """Sprint 9: marca draft como respondido.

    Apaga draft de Redis e arquiva email original. id e o draft_id (mesmo
    id usado pelo email_scan ao popular Redis).
    """
    from fastapi.responses import RedirectResponse
    from workers import briefing_inbox

    ok, _ = verify_token(id, "draft-done", t)
    if not ok:
        return _action_html("Token invalido", "Esse link expirou ou foi alterado.")

    # 1. pop draft do Redis (devolve dict ou None)
    try:
        draft = await state.pop_draft_by_id(id)
    except Exception as exc:
        log.warning("draft-done.pop_fail", id=id, err=str(exc))
        draft = None

    # 2. arquivar email original (se temos coords)
    if draft:
        msg_id = draft.get("gmail_message_id")
        inbox = draft.get("gmail_inbox") or draft.get("inbox") or draft.get("account")
        if msg_id and inbox:
            try:
                from services.gmail_archive import archive_message
                arch_ok, arch_msg = await archive_message(inbox, msg_id)
                if not arch_ok:
                    log.warning("draft-done.archive_failed", id=id, err=arch_msg)
            except Exception as exc:
                log.warning("draft-done.archive_exc", id=id, err=str(exc))
        else:
            log.info("draft-done.no_gmail_coords", id=id)
    else:
        log.info("draft-done.draft_not_found", id=id)

    # 3. regenerar briefing
    try:
        await briefing_inbox.run()
    except Exception as exc:
        log.warning("briefing_inbox refresh FAIL", err=str(exc))

    return RedirectResponse(url=MORNING_BRIEFING_URL, status_code=302)


@app.get("/scheduler/jobs", dependencies=[Depends(require_token)])
def scheduler_jobs() -> dict:
    return {"jobs": native_scheduler.get_jobs()}


# ====== v9.4 PWA dashboard ======
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_STATIC_DIR = _Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/inbox", response_class=FileResponse)
async def inbox_html():
    return FileResponse(str(_STATIC_DIR / "inbox.html"), media_type="text/html")


@app.get("/manifest.json", response_class=FileResponse)
async def manifest():
    return FileResponse(str(_STATIC_DIR / "manifest.json"), media_type="application/json")


@app.get("/sw.js", response_class=FileResponse)
async def service_worker():
    return FileResponse(str(_STATIC_DIR / "sw.js"), media_type="application/javascript")


@app.get("/api/inbox/items")
async def api_inbox_items():
    items = await briefing_db.list_open(limit=200)
    stats = await briefing_db.stats_por_urgencia()
    cleaned = []
    for it in items:
        d = dict(it)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "hex") and not isinstance(v, (bytes, bytearray, str)):
                d[k] = str(v)
        cleaned.append(d)
    full_stats = {"P0": 0, "P1": 0, "P2": 0, "P3": 0, **stats}
    return JSONResponse({"items": cleaned, "stats": full_stats})


@app.get("/api/inbox/resolved")
async def api_inbox_resolved():
    items = await briefing_db.list_recently_resolved(hours=24, limit=50)
    cleaned = []
    for it in items:
        d = dict(it)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "hex") and not isinstance(v, (bytes, bytearray, str)):
                d[k] = str(v)
        cleaned.append(d)
    return JSONResponse({"items": cleaned})


@app.post("/api/inbox/action")
async def api_inbox_action(id: str, action: str, days: int = 7):
    from workers import briefing_inbox
    if action == "done":
        await briefing_db.mark_done(id)
    elif action == "dismiss":
        await briefing_db.mark_dismissed(id)
    elif action == "snooze":
        await briefing_db.snooze(id, days=max(1, min(60, days)))
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    try:
        await briefing_inbox.run()
    except Exception:
        pass
    return JSONResponse({"ok": True})


# ====== v9.5 dashboard endpoints (todos, emails, invoices, summary) ======
from services import todos_db as _todos_db  # noqa: E402
from services import email_inbox_db as _email_db  # noqa: E402
from services import email_drafter as _email_drafter  # noqa: E402
from services import state as _state  # noqa: E402
from pydantic import BaseModel as _BaseModel  # noqa: E402


def _clean(d):
    out = {}
    for k, v in dict(d).items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "hex") and not isinstance(v, (bytes, bytearray, str)):
            out[k] = str(v)
        else:
            out[k] = v
    return out


# ---------- TODOS ----------

class _TodoIn(_BaseModel):
    titulo: str
    detalhe: str | None = None
    prioridade: str = "P2"
    empresa: str | None = None


@app.get("/api/todos")
async def api_todos_list():
    op = await _todos_db.list_open(limit=200)
    dn = await _todos_db.list_recent_done(hours=48, limit=30)
    st = await _todos_db.stats()
    return JSONResponse({
        "open": [_clean(x) for x in op],
        "done": [_clean(x) for x in dn],
        "stats": st,
    })


@app.post("/api/todos")
async def api_todos_create(body: _TodoIn):
    tid = await _todos_db.create(
        titulo=body.titulo, detalhe=body.detalhe,
        prioridade=body.prioridade, empresa=body.empresa,
    )
    return JSONResponse({"ok": True, "id": tid})


@app.post("/api/todos/{todo_id}/complete")
async def api_todos_complete(todo_id: str):
    ok = await _todos_db.complete(todo_id)
    return JSONResponse({"ok": ok})


@app.post("/api/todos/{todo_id}/reopen")
async def api_todos_reopen(todo_id: str):
    ok = await _todos_db.reopen(todo_id)
    return JSONResponse({"ok": ok})


@app.delete("/api/todos/{todo_id}")
async def api_todos_delete(todo_id: str):
    ok = await _todos_db.delete(todo_id)
    return JSONResponse({"ok": ok})


# ---------- EMAILS ----------

@app.get("/api/emails/summary")
async def api_emails_summary():
    summary = await _email_db.summary_por_caixa()
    total = await _email_db.total_pending()
    return JSONResponse({"summary": summary, "total_pending": total})


@app.get("/api/emails/pending")
async def api_emails_pending(account: str | None = None):
    items = await _email_db.list_pending(account=account, limit=100)
    return JSONResponse({"items": [_clean(x) for x in items]})


@app.post("/api/emails/{item_id}/draft")
async def api_emails_draft(item_id: str):
    r = await _email_drafter.generate_for(item_id)
    return JSONResponse(r)


@app.post("/api/emails/{item_id}/done")
async def api_emails_done(item_id: str):
    ok = await _email_db.mark_done(item_id)
    return JSONResponse({"ok": ok})


@app.post("/api/emails/{item_id}/replied")
async def api_emails_replied(item_id: str):
    ok = await _email_db.mark_replied(item_id)
    return JSONResponse({"ok": ok})


@app.post("/api/emails/{item_id}/dismiss")
async def api_emails_dismiss(item_id: str):
    ok = await _email_db.mark_dismissed(item_id)
    return JSONResponse({"ok": ok})


# ---------- INVOICES ----------

@app.get("/api/invoices/overview")
async def api_invoices_overview():
    import json as _json
    archived = await _state.peek_invoices()
    manual = await _state.peek_manual_invoices()
    # peek_* devolvem dicts ja parsed
    return JSONResponse({
        "archived_today": archived,
        "manual_queue": manual,
        "recent_fs": [],
    })


# ---------- DASHBOARD SUMMARY ----------

@app.get("/api/dashboard/summary")
async def api_dashboard_summary():
    inbox = await briefing_db.list_open(limit=200)
    inbox_stats = await briefing_db.stats_por_urgencia()
    todos_stats = await _todos_db.stats()
    email_summary = await _email_db.summary_por_caixa()
    emails_pending = await _email_db.total_pending()
    archived = await _state.peek_invoices()
    manual = await _state.peek_manual_invoices()

    full_inbox_stats = {"P0": 0, "P1": 0, "P2": 0, "P3": 0, **inbox_stats}
    urgentes = [_clean(x) for x in inbox if x.get("urgencia") in ("P0", "P1")][:5]

    return JSONResponse({
        "inbox_stats": full_inbox_stats,
        "todos_stats": todos_stats,
        "email_summary": email_summary,
        "emails_pending": emails_pending,
        "invoices_today_count": len(archived),
        "invoices_manual_count": len(manual),
        "urgentes": urgentes,
    })


@app.get("/api/dashboard/badges")
async def api_dashboard_badges():
    inbox_stats = await briefing_db.stats_por_urgencia()
    inbox_total = sum(inbox_stats.values())
    todos_stats = await _todos_db.stats()
    emails_pending = await _email_db.total_pending()
    return JSONResponse({
        "inbox_total": inbox_total,
        "todos_open": todos_stats.get("total", 0),
        "emails_pending": emails_pending,
    })


# ====== v9.6 learned rules endpoints ======
from services import learned_rules_db as __lr  # noqa: E402


class _LearnIn(_BaseModel):
    type: str
    pattern: str
    cls: str
    notes: str | None = None
    remove_from: dict | None = None  # {from, subject} para LREM na manual queue


@app.post("/api/learn")
async def api_learn(body: _LearnIn):
    try:
        rid = await __lr.add(
            rule_type=body.type, pattern=body.pattern,
            forced_class=body.cls, source="manual", notes=body.notes,
        )
        removed = 0
        # v9.8.1: remover item da manual queue (Redis) se foi indicado
        if body.remove_from:
            import json as _json
            try:
                from services.state import _c as _redis
                c = _redis()
                if c is not None:
                    rf = body.remove_from
                    target_from = (rf.get("from") or "").strip()
                    target_subj = (rf.get("subject") or "").strip()
                    raw = await c.lrange("omnai:invoices:manual_queue", 0, -1) or []
                    for item_str in raw:
                        try:
                            obj = _json.loads(item_str) if isinstance(item_str, str) else item_str
                            if (obj.get("from") or "") == target_from and (obj.get("subject") or "") == target_subj:
                                await c.lrem("omnai:invoices:manual_queue", 1, item_str)
                                removed += 1
                        except Exception:
                            continue
            except Exception as exc:
                log.warning("manual_queue.lrem_failed", err=str(exc))
        return JSONResponse({"ok": True, "id": rid, "removed_from_queue": removed})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/learned")
async def api_learned_list(enabled_only: bool = False):
    items = await __lr.list_all(enabled_only=enabled_only)
    return JSONResponse({"items": [_clean(x) for x in items]})


@app.delete("/api/learned/{rule_id}")
async def api_learned_delete(rule_id: str):
    ok = await __lr.delete(rule_id)
    return JSONResponse({"ok": ok})


@app.post("/api/learned/{rule_id}/disable")
async def api_learned_disable(rule_id: str):
    ok = await __lr.disable(rule_id)
    return JSONResponse({"ok": ok})


@app.post("/api/learned/reload")
async def api_learned_reload():
    stats = await __lr.reload_cache()
    return JSONResponse({"ok": True, "stats": stats})


# ====== v9.8 Telegram bot endpoints ======
from fastapi import Header  # noqa: E402

from services import telegram_bot as __tb  # noqa: E402


@app.post("/api/telegram/webhook")
async def telegram_webhook(payload: dict, x_telegram_bot_api_secret_token: str = Header(default="")):
    import os
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or x_telegram_bot_api_secret_token != expected:
        return JSONResponse({"ok": False, "error": "invalid secret"}, status_code=403)
    try:
        await __tb.process_update(payload)
    except Exception as exc:
        log.warning("telegram.process_update_failed", err=str(exc))
    return JSONResponse({"ok": True})


@app.post("/api/telegram/set-webhook", dependencies=[Depends(require_token)])
async def telegram_set_webhook(url: str | None = None):
    """Configura webhook do Telegram. URL default: ACTIONS_BASE_URL/api/telegram/webhook"""
    import os
    target = url or (os.getenv("ACTIONS_BASE_URL", "https://agents.omnai.pt") + "/api/telegram/webhook")
    r = await __tb.set_webhook(target)
    return JSONResponse(r)


@app.get("/api/telegram/webhook-info", dependencies=[Depends(require_token)])
async def telegram_webhook_info():
    r = await __tb.get_webhook_info()
    return JSONResponse(r)


@app.post("/api/telegram/test", dependencies=[Depends(require_token)])
async def telegram_test():
    """Envia mensagem de teste a todos os subscritores."""
    users = await __tb.list_active_users(urgencia="P0")
    if not users:
        return JSONResponse({"ok": False, "error": "sem subscritores. envia /start ao bot primeiro."})
    sent = 0
    for cid in users:
        r = await __tb.send_message(cid, "🦉 Mensagem de teste do OMNAI Inbox Bot. Se vês isto, o canal funciona.")
        if r.get("ok"):
            sent += 1
    return JSONResponse({"ok": True, "sent": sent, "total": len(users)})
