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


@app.get("/", response_class=FileResponse)
async def raiz_html():
    return FileResponse(str(_STATIC_DIR / "home.html"), media_type="text/html")


@app.get("/inbox")
async def inbox_html():
    """A app antiga de 5 separadores deu lugar ao Hoje, Fazer e Faturas.

    Redirecciona em vez de servir, porque o manifesto apontava para aqui e o
    icone instalado no telemovel continuava a abrir a versao antiga.
    O ficheiro static/inbox.html fica, para se poder voltar atras.
    """
    from starlette.responses import RedirectResponse as _Redirect
    return _Redirect(url="/hoje", status_code=307)


@app.get("/inbox-antigo", response_class=FileResponse)
async def inbox_antigo_html():
    return FileResponse(str(_STATIC_DIR / "inbox.html"), media_type="text/html")


@app.get("/favicon.ico", response_class=FileResponse)
async def favicon():
    """O browser pede /favicon.ico a raiz, nao ao /static."""
    return FileResponse(str(_STATIC_DIR / "favicon.ico"),
                        media_type="image/x-icon")


@app.get("/instalar", response_class=FileResponse)
async def instalar_html():
    return FileResponse(str(_STATIC_DIR / "instalar.html"),
                        media_type="text/html")


@app.get("/manifest.json", response_class=FileResponse)
async def manifest():
    return FileResponse(str(_STATIC_DIR / "manifest.json"), media_type="application/json")


@app.get("/sw.js", response_class=FileResponse)
async def service_worker():
    return FileResponse(str(_STATIC_DIR / "sw.js"), media_type="application/javascript")


@app.get("/hoje", response_class=FileResponse)
async def hoje_html():
    return FileResponse(str(_STATIC_DIR / "hoje.html"), media_type="text/html")


@app.get("/api/hoje")
async def api_hoje(limit: int = 60):
    """Fila unica do ecra Hoje. Ver briefing_db.list_hoje para a ordenacao."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    def _link_util(link, meta):
        """O Notion e armazem de maquinas, nao destino de leitura humana.

        Ordem: o documento original (DR/BASE) primeiro, depois o link_origem
        se nao for do Notion. Um link que obriga a login nao serve de nada.
        """
        return _link_humano(link, meta)

    items = await briefing_db.list_hoje(limit=limit)
    agora = _dt.now(_tz.utc)
    saida = []
    for it in items:
        d = dict(it)
        meta = d.get("metadata")
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        meta = meta or {}
        espera = d.get("espera_desde")
        dias = None
        if espera is not None:
            try:
                dias = max(0, (agora - espera).days)
            except Exception:
                dias = None
        prazo = d.get("prazo")
        dias_prazo = None
        if prazo is not None:
            try:
                dias_prazo = (prazo - agora.date()).days
            except Exception:
                dias_prazo = None
        saida.append({
            "id": str(d.get("id")),
            "tipo": d.get("tipo"),
            "urgencia": d.get("urgencia"),
            "empresa": d.get("empresa"),
            "titulo": d.get("titulo"),
            "detalhe": d.get("detalhe"),
            "link": _link_util(d.get("link_origem"), meta),
            "entidade": meta.get("entidade"),
            "valor_base": meta.get("valor_base"),
            "referencia": meta.get("referencia"),
            "plataforma": meta.get("plataforma"),
            "de": meta.get("from"),
            "motivo": meta.get("reason"),
            "conta": meta.get("account"),
            "dias_espera": dias,
            "dias_prazo": dias_prazo,
        })
    return JSONResponse({"items": saida, "total": len(saida)})


@app.post("/api/item/{item_id}/rascunho")
async def api_item_rascunho(item_id: str):
    """Cria um rascunho de resposta no Gmail, dentro da conversa original.

    Nao envia nada. Devolve o URL para o ecra abrir o rascunho ja escrito,
    para o David ler, corrigir e mandar. So funciona em contas Gmail: as
    contas IMAP nao tem onde guardar o rascunho.
    """
    import asyncio as _aio

    from services import briefing_db as _bdb
    from services import gmail as _gm
    from services.classifier import extract_email as _extrai
    from services.drafter import draft_response as _escreve

    pool = await _bdb._get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT metadata FROM briefing_items WHERE id = $1::uuid", item_id)
        except Exception:
            row = None
    if row is None:
        return JSONResponse({"ok": False, "erro": "item nao encontrado"},
                            status_code=404)

    meta = _meta_dict(row["metadata"])
    conta = meta.get("account") or meta.get("gmail_inbox") or ""
    mid = meta.get("gmail_message_id") or ""
    if conta not in _GMAIL_ACCOUNTS or not mid:
        return JSONResponse(
            {"ok": False, "erro": "este item nao vem de uma conta Gmail"},
            status_code=400)

    try:
        resumo = await _aio.to_thread(_gm.summarize_message, conta, mid)
        if not resumo:
            raise RuntimeError("nao consegui ler o email original")
        assunto = resumo.get("subject") or meta.get("subject") or ""
        resposta = assunto if assunto.lower().startswith("re:") else "Re: " + assunto
        texto = await _escreve(conta, resumo.get("from", ""), assunto,
                               resumo.get("body", ""))
        draft = await _aio.to_thread(
            _gm.create_draft, conta, _extrai(resumo.get("from", "")),
            resposta, texto, resumo.get("thread_id"),
            resumo.get("message_id_header"))
    except Exception as exc:
        log.warning("item.rascunho_falhou", id=item_id, err=str(exc))
        return JSONResponse({"ok": False, "erro": str(exc)[:180]}, status_code=502)

    msg_id = (draft.get("message") or {}).get("id") or ""
    thread = (draft.get("message") or {}).get("threadId") or resumo.get("thread_id") or ""
    return JSONResponse({
        "ok": True,
        "url": ("https://mail.google.com/mail/u/?authuser=%s#drafts?compose=%s"
                % (conta, msg_id)) if msg_id else None,
        "url_conversa": ("https://mail.google.com/mail/u/?authuser=%s#all/%s"
                         % (conta, thread)) if thread else None,
        "texto": texto,
    })


@app.get("/faturas", response_class=FileResponse)
async def faturas_html():
    return FileResponse(str(_STATIC_DIR / "faturas.html"), media_type="text/html")


@app.get("/api/faturas/resumo")
async def api_faturas_resumo():
    from services import faturas_db
    linhas = await faturas_db.resumo()
    for l in linhas:
        for k in ("eur", "usd"):
            l[k] = float(l[k] or 0)
    return JSONResponse({"linhas": linhas})


@app.get("/api/faturas")
async def api_faturas(mes: str | None = None, empresa: str | None = None,
                      estado: str | None = "por_validar", limit: int = 200):
    from services import faturas_db
    itens = await faturas_db.listar(mes=mes, empresa=empresa, estado=estado, limit=limit)
    saida = []
    for i in itens:
        d = dict(i)
        d["id"] = str(d["id"])
        if d.get("data_fatura") is not None:
            d["data_fatura"] = d["data_fatura"].isoformat()
        d["valor"] = float(d["valor"]) if d.get("valor") is not None else None
        saida.append(d)
    return JSONResponse({"itens": saida, "total": len(saida),
                         "empresas": list(faturas_db.EMPRESAS)})


@app.post("/api/faturas/{fatura_id}/accao")
async def api_faturas_accao(fatura_id: str, accao: str,
                            empresa: str | None = None, motivo: str | None = None):
    from services import faturas_db
    if accao not in ("validar", "ignorar", "reabrir", "empresa"):
        return JSONResponse({"error": "accao desconhecida"}, status_code=400)
    aplicado = await faturas_db.accao(fatura_id, accao, empresa=empresa, motivo=motivo)
    return JSONResponse({"ok": True, "aplicado": bool(aplicado)})


@app.get("/api/faturas/{fatura_id}/ficheiro")
async def api_fatura_ficheiro(fatura_id: str):
    """Serve o PDF da fatura, para ser visto antes de validar."""
    import os as _os
    from pathlib import Path as _P
    from services import faturas_db

    pool = await faturas_db._get_pool()
    async with pool.acquire() as conn:
        rel = await conn.fetchval(
            "SELECT ficheiro FROM faturas WHERE id = $1::uuid", fatura_id
        )
    if not rel:
        return JSONResponse({"error": "nao encontrada"}, status_code=404)

    raiz = _P(_os.getenv("FATURAS_DIR", "/faturas")).resolve()
    caminho = (raiz / rel).resolve()
    # Guarda contra travessia de directorios: o caminho tem de ficar sob a raiz.
    if not str(caminho).startswith(str(raiz)) or not caminho.is_file():
        return JSONResponse({"error": "ficheiro indisponivel"}, status_code=404)

    return FileResponse(
        str(caminho), media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{caminho.name}"'},
    )


@app.get("/api/faturas/pacote")
async def api_faturas_pacote(empresa: str, mes: str):
    """Pre-visualizacao do pacote: o que seria entregue, sem entregar nada."""
    from services import faturas_db
    itens = await faturas_db.para_pacote(empresa, mes)
    total_eur = sum(float(i["valor"] or 0) for i in itens if i["moeda"] == "EUR")
    total_usd = sum(float(i["valor"] or 0) for i in itens if i["moeda"] == "USD")
    return JSONResponse({
        "empresa": empresa, "mes": mes, "n": len(itens),
        "total_eur": round(total_eur, 2), "total_usd": round(total_usd, 2),
        "itens": [{
            "id": str(i["id"]), "fornecedor": i["fornecedor"],
            "data": i["data_fatura"].isoformat() if i["data_fatura"] else None,
            "valor": float(i["valor"]) if i["valor"] is not None else None,
            "moeda": i["moeda"], "ficheiro": i["ficheiro"],
        } for i in itens],
    })


@app.get("/fazer", response_class=FileResponse)
async def fazer_html():
    return FileResponse(str(_STATIC_DIR / "fazer.html"), media_type="text/html")


from pydantic import BaseModel as _BaseModelFazer  # noqa: E402


class _TarefaIn(_BaseModelFazer):
    texto: str
    empresa: str | None = None
    prazo: str | None = None


@app.get("/api/fazer")
async def api_fazer_listar(feitas: bool = False):
    from services import fazer_db
    itens = await fazer_db.listar(incluir_feitas=feitas)
    saida = []
    for t in itens:
        d = dict(t)
        d["id"] = str(d["id"])
        for k in ("prazo", "criado_em", "completado_em"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        saida.append(d)
    return JSONResponse({"itens": saida, "total": len(saida)})


@app.post("/api/fazer")
async def api_fazer_criar(dados: _TarefaIn):
    """Uma linha de texto basta. O prazo e a empresa saem do proprio texto."""
    from datetime import date as _date

    from services import fazer_db
    if not (dados.texto or "").strip():
        return JSONResponse({"error": "texto vazio"}, status_code=400)
    prazo = None
    if dados.prazo:
        try:
            prazo = _date.fromisoformat(dados.prazo)
        except ValueError:
            prazo = None
    t = await fazer_db.criar(dados.texto, empresa=dados.empresa, prazo=prazo)
    t["id"] = str(t["id"])
    for k in ("prazo", "criado_em"):
        if t.get(k) is not None:
            t[k] = t[k].isoformat()
    return JSONResponse({"ok": True, "tarefa": t})


@app.post("/api/fazer/{todo_id}/accao")
async def api_fazer_accao(todo_id: str, accao: str, prazo: str | None = None,
                          empresa: str | None = None):
    from datetime import date as _date

    from services import fazer_db
    if accao not in ("concluir", "reabrir", "apagar", "prazo", "empresa"):
        return JSONResponse({"error": "accao desconhecida"}, status_code=400)
    p = None
    if prazo:
        try:
            p = _date.fromisoformat(prazo)
        except ValueError:
            p = None
    aplicado = await fazer_db.accao(todo_id, accao, prazo=p, empresa=empresa)
    return JSONResponse({"ok": True, "aplicado": bool(aplicado)})


try:
    from services.gmail import GMAIL_ACCOUNTS as _GMAIL_ACCOUNTS
except Exception:  # pragma: no cover
    _GMAIL_ACCOUNTS = {}


def _meta_dict(valor) -> dict:
    """O asyncpg devolve jsonb como texto conforme o caminho de leitura."""
    import json as _json
    if isinstance(valor, str):
        try:
            valor = _json.loads(valor)
        except Exception:
            return {}
    return valor if isinstance(valor, dict) else {}


def _link_humano(link, meta: dict):
    """Um link so serve se abrir onde o David consegue trabalhar.

    O Notion obriga a login e nao abre na app. E o email_scan chegou a
    escrever URLs de Gmail para contas que nao sao Gmail (sapo), que
    rebentam. Por isso o link e reconstruido a partir da metadata sempre
    que da, em vez de confiar no link_origem.
    """
    meta = meta or {}
    # 07-08-2026: o arquivo_faturas escreve a conta como "inbox", o email_scan
    # escreve "account". Faltava aqui o "inbox" e os cartoes de factura
    # perdiam o link do Gmail que ja tinham guardado.
    conta = (meta.get("account") or meta.get("gmail_inbox")
             or meta.get("inbox") or "").strip()
    mid = (meta.get("gmail_message_id") or meta.get("thread_id") or "").strip()
    if conta in _GMAIL_ACCOUNTS and mid:
        return "https://mail.google.com/mail/u/?authuser=%s#all/%s" % (conta, mid)
    original = (meta.get("url_original") or "").strip()
    if original:
        return original
    link = (link or "").strip()
    if not link:
        return None
    if "notion.so" in link or "notion.com" in link:
        return None
    if "mail.google.com" in link and conta not in _GMAIL_ACCOUNTS:
        return None
    return link


def _prioridades_lista(brief) -> list:
    """O asyncpg devolve jsonb como texto; o ecra precisa de lista."""
    import json as _json
    if not brief:
        return []
    valor = brief["prioridades"]
    if isinstance(valor, str):
        try:
            valor = _json.loads(valor)
        except Exception:
            return []
    return valor if isinstance(valor, list) else []


# --- 19-08-2026: a prosa vale para o conjunto de itens que descreve -------
# Estado do regenerador. O David clica varias vezes seguidas na Home; nao
# vale uma chamada ao LLM por clique.
_RESUMO_REGEN = {"a_correr": False, "ultimo": 0.0}


def _ids_descritos(brief) -> list:
    """Ids das prioridades que a prosa descreve."""
    saida = []
    for p in _prioridades_lista(brief):
        if isinstance(p, dict) and p.get("id"):
            saida.append(str(p["id"]))
    return saida


def _ids_json(valor) -> list:
    """O asyncpg devolve jsonb como texto conforme o caminho de leitura."""
    import json as _json
    if isinstance(valor, str):
        try:
            valor = _json.loads(valor or "[]")
        except Exception:
            return []
    return [str(x) for x in valor] if isinstance(valor, list) else []


async def _algum_ja_fechado(conn, ids: list) -> bool:
    """True se algum destes itens ja nao esta aberto.

    Chegar itens novos nao conta: um paragrafo incompleto perde uma
    novidade, um paragrafo falso manda fazer o que ja esta feito.
    """
    if not ids:
        return False
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM briefing_items "
            "WHERE id = ANY($1::uuid[]) AND status <> 'open'", ids)
        return bool(n)
    except Exception as exc:
        log.warning("resumo.validar_falhou", err=str(exc)[:150])
        return False


async def _regenerar_resumo(dia) -> None:
    """Reescreve o paragrafo a partir do que continua aberto.

    Guarda-se o conjunto de ids a que o texto novo corresponde, para a
    leitura seguinte saber se ainda e verdade em vez de adivinhar.
    """
    import json as _json
    import time as _time

    if _RESUMO_REGEN["a_correr"]:
        return
    if _time.time() - _RESUMO_REGEN["ultimo"] < 90:
        return
    _RESUMO_REGEN["a_correr"] = True
    try:
        from workers.briefing_inbox import _resumo_executivo

        linhas = await briefing_db.list_hoje(limit=40)
        abertos = [dict(x) for x in linhas]
        stats = {}
        for x in abertos:
            u = x.get("urgencia") or "P2"
            stats[u] = stats.get(u, 0) + 1

        texto = await _resumo_executivo(abertos, stats)
        # So os que o LLM viu de facto. _resumo_executivo mostra-lhe items[:8];
        # carimbar a lista toda fazia a prosa cair a cada mexida em qualquer
        # canto, incluindo em itens que ela nunca poderia ter nomeado.
        ids = [str(x["id"]) for x in abertos[:8]]

        pool = await briefing_db._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE briefing_dia SET resumo_vivo = $2, "
                "resumo_vivo_ids = $3::jsonb, resumo_vivo_em = now() "
                "WHERE data = $1", dia, texto, _json.dumps(ids))
        log.info("resumo.regenerado", dia=str(dia), itens=len(ids))
    except Exception as exc:
        log.warning("resumo.regenerar_falhou", err=str(exc)[:200])
    finally:
        _RESUMO_REGEN["a_correr"] = False
        _RESUMO_REGEN["ultimo"] = __import__("time").time()



@app.get("/home", response_class=FileResponse)
async def home_html():
    return FileResponse(str(_STATIC_DIR / "home.html"), media_type="text/html")


@app.get("/api/home")
async def api_home():
    """Tudo o que a Home precisa, numa chamada e sem tocar no LLM.

    O resumo e as prioridades vêm da tabela briefing_dia, escrita de manhã
    pelo worker. Se ainda não houver briefing hoje, devolve o de ontem
    marcado como tal, em vez de mentir com uma página vazia.
    """
    from datetime import date as _d

    from services import briefing_db

    pool = await briefing_db._get_pool()
    async with pool.acquire() as conn:
        brief = await conn.fetchrow(
            "SELECT data, resumo, prioridades, gerado_em, "
            "resumo_vivo, resumo_vivo_ids FROM briefing_dia "
            "ORDER BY data DESC LIMIT 1"
        ) if await conn.fetchval(
            "SELECT to_regclass('public.briefing_dia') IS NOT NULL"
        ) else None

        urgentes = await conn.fetch(
            """
            SELECT id, titulo, empresa, urgencia, tipo,
                   CASE WHEN metadata->>'prazo_propostas' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                        THEN (metadata->>'prazo_propostas')::date END AS prazo
              FROM briefing_items
             WHERE status = 'open' AND urgencia = 'P0'
             ORDER BY 5 NULLS LAST, criado_em
             LIMIT 6
            """
        )
        abertos = await conn.fetchval(
            "SELECT count(*) FROM briefing_items WHERE status='open'")
        tarefas = await conn.fetch(
            """
            SELECT id, titulo, empresa, prazo
              FROM user_todos
             WHERE status = 'open' AND prazo IS NOT NULL AND prazo <= current_date
             ORDER BY prazo
             LIMIT 8
            """
        )
        tarefas_abertas = await conn.fetchval(
            "SELECT count(*) FROM user_todos WHERE status='open'")
        faturas_validar = await conn.fetchval(
            "SELECT count(*) FROM faturas WHERE estado='por_validar'"
        ) if await conn.fetchval(
            "SELECT to_regclass('public.faturas') IS NOT NULL") else 0

        # As prioridades sao guardadas de manha com id e link. Ate hoje o
        # ecra so mostrava o texto, o que obrigava a ir procurar o item ao
        # Hoje. Aqui vai buscar-se o estado actual: o que ja foi despachado
        # deixa de aparecer, e o que falta ganha link e accoes.
        prioridades = []
        for _p in _prioridades_lista(brief):
            if not isinstance(_p, dict):
                continue
            _pid = str(_p.get("id") or "").strip()
            _r = None
            if _pid:
                try:
                    _r = await conn.fetchrow(
                        "SELECT id, titulo, tipo, empresa, status, link_origem, "
                        "metadata FROM briefing_items WHERE id = $1::uuid", _pid)
                except Exception:
                    _r = None
            if _r is None:
                prioridades.append({"id": _pid or None, "texto": _p.get("texto")})
                continue
            if _r["status"] != "open":
                continue
            _m = _meta_dict(_r["metadata"])
            _conta = _m.get("account") or _m.get("gmail_inbox") or ""
            prioridades.append({
                "id": str(_r["id"]),
                "texto": _p.get("texto") or _r["titulo"],
                "tipo": _r["tipo"],
                "empresa": _r["empresa"],
                "assunto": _m.get("subject"),
                "de": _m.get("from"),
                "link": _link_humano(_r["link_origem"], _m),
                "rascunho": bool(_m.get("gmail_message_id"))
                            and _conta in _GMAIL_ACCOUNTS,
            })

    # 19-08-2026: um paragrafo vale para o conjunto de itens sobre o qual foi
    # escrito, e para mais nenhum. Sai um do aberto e deixa de ser verdade.
    # Antes disto bastava sobrar UM item aberto para a prosa continuar a ser
    # mostrada, ainda que abrisse com um tema ja despachado.
    _ids_desc = set(_ids_descritos(brief))
    _ids_abertos = set(str(p["id"]) for p in prioridades if p.get("id"))
    _prio_descritas = len([p for p in _prioridades_lista(brief)
                           if isinstance(p, dict)])
    _prio_abertas = len(prioridades)
    _resumo_obsoleto = bool(brief and _ids_desc and _ids_desc != _ids_abertos)

    _resumo_texto = brief["resumo"] if brief and not _resumo_obsoleto else None
    _a_actualizar = False
    if brief and _resumo_obsoleto:
        _pool_home = await briefing_db._get_pool()
        # Vale a prosa reescrita depois, se corresponder exactamente ao que
        # esta aberto agora. Caso contrario nao se mostra nada e pede-se uma
        # nova: um vazio custa menos do que uma ordem para refazer o feito.
        _vivo_ids = _ids_json(brief["resumo_vivo_ids"])
        async with _pool_home.acquire() as conn_home:
            _vivo_ok = bool(brief["resumo_vivo"] and _vivo_ids) and not (
                await _algum_ja_fechado(conn_home, _vivo_ids))
        if _vivo_ok:
            _resumo_texto = brief["resumo_vivo"]
            _resumo_obsoleto = False
        else:
            import asyncio as _aio_regen
            _a_actualizar = True
            _aio_regen.create_task(_regenerar_resumo(brief["data"]))

    hoje = _d.today()
    return JSONResponse({
        "data": hoje.isoformat(),
        "dia_do_mes": hoje.day,
        "em_fecho": hoje.day <= 10,
        "briefing": {
            "data": brief["data"].isoformat() if brief else None,
            "de_hoje": bool(brief and brief["data"] == hoje),
            # Prosa sobre trabalho ja feito e pior do que nao ter prosa.
            "resumo": _resumo_texto,
            "resumo_obsoleto": _resumo_obsoleto,
            "resumo_a_actualizar": _a_actualizar,
            "prioridades_descritas": _prio_descritas,
            "prioridades_fechadas": max(0, _prio_descritas - _prio_abertas),
            "prioridades": prioridades,
        },
        "urgentes": [{
            "id": str(u["id"]), "titulo": u["titulo"], "empresa": u["empresa"],
            "tipo": u["tipo"],
            "prazo": u["prazo"].isoformat() if u["prazo"] else None,
        } for u in urgentes],
        "tarefas_hoje": [{
            "id": str(t["id"]), "titulo": t["titulo"], "empresa": t["empresa"],
            "prazo": t["prazo"].isoformat() if t["prazo"] else None,
        } for t in tarefas],
        "contagens": {
            "hoje": abertos or 0,
            "fazer": tarefas_abertas or 0,
            "faturas": faturas_validar or 0,
        },
    })


@app.get("/apps", response_class=FileResponse)
async def apps_html():
    return FileResponse(str(_STATIC_DIR / "plataformas.html"), media_type="text/html")


@app.get("/empresas", response_class=FileResponse)
async def empresas_html():
    return FileResponse(str(_STATIC_DIR / "empresas.html"), media_type="text/html")


class _CampoEmpresaIn(_BaseModelFazer):
    empresa: str
    campo: str
    valor: str | None = None
    ordem: int = 100


@app.get("/api/empresas")
async def api_empresas():
    from services import empresas_db
    linhas = await empresas_db.listar()
    return JSONResponse({
        "dados": [{
            "id": str(d["id"]), "empresa": d["empresa"], "campo": d["campo"],
            "valor": d["valor"], "ordem": d["ordem"], "nota": d["nota"],
        } for d in linhas],
        "empresas": list(empresas_db.EMPRESAS),
    })


@app.post("/api/empresas/campo")
async def api_empresas_campo(dados: _CampoEmpresaIn):
    from services import empresas_db
    ok = await empresas_db.gravar(dados.empresa, dados.campo, dados.valor,
                                  ordem=dados.ordem)
    if not ok:
        return JSONResponse({"error": "empresa ou campo invalido"}, status_code=400)
    return JSONResponse({"ok": True})


@app.delete("/api/empresas/{dado_id}")
async def api_empresas_apagar(dado_id: str):
    from services import empresas_db
    return JSONResponse({"ok": True, "aplicado": await empresas_db.apagar(dado_id)})


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


# 31-07-2026: so um regen do briefing de cada vez. Sem isto, varios cliques
# seguidos lancavam varias reconstrucoes da pagina Notion em paralelo.
_regen_em_curso = False


async def _regen_briefing_em_fundo():
    global _regen_em_curso
    if _regen_em_curso:
        return
    _regen_em_curso = True
    try:
        from workers import briefing_inbox
        await briefing_inbox.run()
    except Exception as exc:
        log.warning("briefing_inbox.regen_falhou", err=str(exc)[:200])
    finally:
        _regen_em_curso = False


@app.post("/api/inbox/action")
async def api_inbox_action(id: str, action: str, days: int = 7):
    """Responde assim que a base fica gravada.

    A regeneracao do briefing (que fala com o Notion e demorava ate 107s)
    passou para segundo plano. O botao nao espera por ela.
    """
    import asyncio as _asyncio

    if action == "done":
        aplicado = await briefing_db.mark_done(id)
    elif action == "dismiss":
        aplicado = await briefing_db.mark_dismissed(id)
    elif action == "snooze":
        aplicado = await briefing_db.snooze(id, days=max(1, min(60, days)))
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)

    # O email correspondente sai tambem de pendente. Sem isto as duas tabelas
    # divergem e o separador Emails contradiz o ecra Hoje.
    if aplicado and action in ("done", "dismiss"):
        try:
            await briefing_db.sincronizar_email_inbox(id, action)
        except Exception as exc:
            log.warning("email_inbox.sync_falhou", err=str(exc)[:200])

    # "aplicado" diz se a linha mudou mesmo. Antes devolvia sempre ok:true,
    # o que escondia falhas silenciosas.
    return JSONResponse({"ok": True, "aplicado": bool(aplicado)})


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


# 07-08-2026: sem Cache-Control o browser inventa quanto tempo guarda a
# pagina, e o service worker nunca chega a rede porque o proprio fetch e
# servido da cache HTTP. Com no-cache, o etag decide, e o custo e um 304.
@app.middleware("http")
async def _revalidar_paginas(request, call_next):
    resposta = await call_next(request)
    caminho = request.url.path
    tipo = resposta.headers.get("content-type", "")
    if (caminho in ("/sw.js", "/manifest.json")
            or caminho.endswith(".html")
            or tipo.startswith("text/html")):
        resposta.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resposta


# ====== 08-09-2026: servidor MCP + portao de sessao da PWA ======
# mcp_server: Claude (claude.ai / Cowork / Code) le as caixas Gmail, faturas,
# fila Hoje e tarefas por JSON-RPC em /mcp, com token em /secrets/mcp_token.txt.
# pwa_gate: fecha a app ao publico quando existir /secrets/pwa_gate.json.
from mcp_server import router as _mcp_router  # noqa: E402
app.include_router(_mcp_router)
import pwa_gate as _pwa_gate  # noqa: E402
_pwa_gate.install(app)


# ====== 08-09-2026 (2): worker revolut-sync (movimentos Revolut + reconciliacao) ======
from workers import revolut_sync as _revolut_sync  # noqa: E402
WORKERS["revolut-sync"] = _revolut_sync.run

# 11-09-2026: fecho mensal e resposta a contabilidade sem passar pelo Claude.
from workers import fecho_pacote as _fecho_pacote  # noqa: E402
from workers import resposta_contabilidade as _resposta_contabilidade  # noqa: E402
WORKERS["fecho-pacote"] = _fecho_pacote.run
WORKERS["resposta-contabilidade"] = _resposta_contabilidade.run

# 11-09-2026: o Drive deixa de ser um sitio onde as facturas se perdem.
from workers import drive_sync as _drive_sync  # noqa: E402
WORKERS["drive-sync"] = _drive_sync.run
