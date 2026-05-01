"""Worker: check-deadlines-legais | Beatriz | Legal

Cron: Quartas 10:09 UTC.

Sprint 5: emite cards em briefing_items para cada deadline na janela
(VENCIDO/CRITICO/MEDIO/BAIXO -> P0/P1/P2 conforme dias). Mantem sub-pagina
Notion, tarefas criticas, email e Activity Log existentes.
"""
from __future__ import annotations

from datetime import date, timedelta

import structlog

from services import notion_ext
from services.briefing_emit import emit_briefing, page_url_from_id
from services.email_notify import send_notification
from services.llm import generate
from services.notion import NotionClient
from services.notion_ext import bullet, heading, paragraph

log = structlog.get_logger()


CONTRATOS_DB = "f5dca372-90dc-4157-9725-70d0a03072d9"
TAREFAS_DB = "0135c7f8-2823-4287-9556-7968fd36998d"
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"

DIAS_FUTURO = 30

WORKER_NAME = "check-deadlines-legais"

EMPRESAS_CANONICAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")

SYSTEM_BEATRIZ = (
    "Es a Beatriz, Legal & Compliance da OMNAI. Rever semanalmente deadlines legais. "
    "Responde em portugues europeu, tu directo, sem cliches, sem travessoes. Maximo 250 palavras."
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _classificar(dias: int) -> str:
    if dias < 0:
        return "VENCIDO"
    if dias < 7:
        return "CRITICO"
    if dias < 15:
        return "MEDIO"
    return "BAIXO"


def _urgencia_pelos_dias(dias: int) -> str:
    """Mapeia dias-restantes para P0/P1/P2.

    VENCIDO ou < 7 dias -> P0
    < 30 dias           -> P1
    senao              -> P2
    """
    if dias < 7:
        return "P0"
    if dias < 30:
        return "P1"
    return "P2"


def _normalizar_empresa(raw: str) -> str:
    if not raw:
        return "OMNAI"
    s = raw.strip()
    low = s.lower()
    if low.startswith("omn"):
        return "OMNAI"
    if low.startswith("prev"):
        return "Previnsa"
    if low.startswith("jms") or "jm soares" in low or low.startswith("jm "):
        return "JMSoares"
    if low.startswith("sop"):
        return "Sopato"
    if low.startswith("pess"):
        return "Pessoal"
    return s if s in EMPRESAS_CANONICAS else "OMNAI"


def _extrair(row: dict, hoje: date) -> dict | None:
    props = row.get("properties", {})
    tprop = next((v for v in props.values() if v.get("type") == "title"), None)
    titulo = "".join(t.get("plain_text", "") for t in (tprop or {}).get("title", []))
    if not titulo:
        return None

    dval = (props.get("Data limite", {}).get("date") or {}).get("start")
    dlim = _parse_date(dval)
    if dlim is None:
        return None
    if not (date.today() - timedelta(days=10) <= dlim <= hoje + timedelta(days=DIAS_FUTURO)):
        return None

    dias = (dlim - hoje).days
    empresa = ""
    ep = props.get("Empresa", {}) or props.get("Entidade", {})
    if ep.get("type") == "select":
        empresa = (ep.get("select") or {}).get("name", "")

    tipo = ""
    tp = props.get("Tipo", {})
    if tp.get("type") == "select":
        tipo = (tp.get("select") or {}).get("name", "")

    return {
        "row_id": row.get("id"),
        "url": row.get("url") or page_url_from_id(row.get("id")),
        "titulo": titulo,
        "empresa": empresa,
        "tipo": tipo,
        "data_limite": dlim,
        "dias": dias,
        "severidade": _classificar(dias),
    }


async def _claude_resumo(itens: list[dict]) -> str:
    linhas = "\n".join(
        f"- {i['severidade']} | {i['dias']:+d}d | {i['empresa']} | {i['tipo']} | {i['titulo']}"
        for i in itens
    )
    prompt = (
        f"Itens com deadline nos proximos {DIAS_FUTURO} dias:\n\n{linhas}\n\n"
        "Compor briefing:\n"
        "1) Resumo em 2-3 frases\n"
        "2) Accoes para CRITICO (<7d)\n"
        "3) Planear MEDIO (7-15d)\n"
    )
    try:
        return await generate(system=SYSTEM_BEATRIZ, prompt=prompt, max_tokens=800)
    except Exception as exc:
        log.error("claude generate FAIL", err=str(exc))
        return "Nao foi possivel gerar resumo automatico esta semana."


async def _emitir_cards_deadlines(itens: list[dict]) -> int:
    """Emite 1 card por deadline. Chave (contrato_id, year_month da data limite)."""
    n = 0
    for i in itens:
        contrato_id = (i.get("row_id") or "").replace("-", "") or "unknown"
        dlim = i["data_limite"]
        ym = f"{dlim.year}-{dlim.month:02d}"
        empresa = _normalizar_empresa(i.get("empresa", ""))
        urgencia = _urgencia_pelos_dias(i["dias"])
        titulo = f"[{i['severidade']}] {i['titulo']} | {dlim.isoformat()} ({i['dias']:+d}d)"
        detalhe = (
            f"Empresa: {empresa}\n"
            f"Tipo: {i.get('tipo') or '-'}\n"
            f"Data limite: {dlim.isoformat()} ({i['dias']:+d}d)\n"
            f"Severidade: {i['severidade']}"
        )
        ok = await emit_briefing(
            tipo="deadline_legal",
            titulo=titulo[:180],
            detalhe=detalhe,
            urgencia=urgencia,
            empresa=empresa,
            chave_parts=(contrato_id, ym),
            link_origem=i.get("url"),
            metadata={
                "contrato_id": i.get("row_id"),
                "tipo_documento": i.get("tipo"),
                "data_limite": dlim.isoformat(),
                "dias_restantes": i["dias"],
                "severidade": i["severidade"],
            },
            worker_name=WORKER_NAME,
        )
        if ok:
            n += 1
    return n


async def run() -> dict:
    hoje = date.today()

    async with NotionClient() as nc:
        try:
            resp = await nc.query_data_source(
                data_source_id=CONTRATOS_DB,
                filter_={
                    "and": [
                        {"property": "Data limite", "date": {"on_or_after": (hoje - timedelta(days=10)).isoformat()}},
                        {"property": "Data limite", "date": {"on_or_before": (hoje + timedelta(days=DIAS_FUTURO)).isoformat()}},
                    ]
                },
            )
            rows = resp.get("results", [])
        except Exception as exc:
            log.warning("query Contratos filter FAIL, fallback all", err=str(exc))
            try:
                resp = await nc.query_data_source(data_source_id=CONTRATOS_DB, page_size=100)
                rows = resp.get("results", [])
            except Exception as exc2:
                log.error("query Contratos FAIL", err=str(exc2))
                rows = []

    itens = [i for r in rows if (i := _extrair(r, hoje)) is not None]
    itens.sort(key=lambda x: x["dias"])

    if not itens:
        await notion_ext.create_database_row(
            data_source_id=ACTIVITY_LOG_DB,
            title=f"check-deadlines-legais | {hoje.isoformat()} | 0 deadlines",
        )
        return {"status": "ok", "total": 0, "criticos": 0, "cards_emitidos": 0}

    resumo = await _claude_resumo(itens)

    blocks = [paragraph(resumo), heading(2, "Lista completa")]
    for i in itens:
        label = (
            f"[{i['severidade']}] {i['data_limite'].isoformat()} ({i['dias']:+d}d) | "
            f"{i['empresa']} | {i['tipo']} | {i['titulo']}"
        )
        blocks.append(bullet(label, link=i.get("url")))

    ano, semana, _ = hoje.isocalendar()
    subpage_id = await notion_ext.create_child_page(
        parent_page_id=MORNING_BRIEFING_PAGE,
        title=f"Deadlines Legais S{semana}/{ano}",
        children=blocks,
    )

    criticos = [i for i in itens if i["severidade"] in ("CRITICO", "VENCIDO")]
    tarefas_criadas = 0
    for i in criticos[:3]:
        pid = await notion_ext.create_database_row(
            data_source_id=TAREFAS_DB,
            title=f"[LEGAL {i['severidade']}] {i['titulo']} | {i['data_limite'].isoformat()}",
        )
        if pid:
            tarefas_criadas += 1

    cards_emitidos = await _emitir_cards_deadlines(itens)

    email_ok = False
    if criticos:
        ul = "".join(
            f"<li><b>{i['titulo']}</b> ({i['empresa']}) | {i['data_limite'].isoformat()} ({i['dias']:+d}d)</li>"
            for i in criticos
        )
        html = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui;color:#111;">
<h2 style="color:#b00020;">{len(criticos)} deadline(s) legais CRITICOS</h2>
<p>{resumo}</p>
<ul>{ul}</ul>
<p style="color:#555;font-size:12px;">Beatriz | Legal & Compliance | OMNAI Agent Team</p>
</body></html>"""
        email_ok = (await send_notification(
            subject=f"[OMNAI Legal] {len(criticos)} deadline(s) CRITICOS nos proximos 7 dias",
            html_body=html,
        )) is not None

    await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=f"check-deadlines-legais | {len(itens)} total | {len(criticos)} criticos | {cards_emitidos} cards",
    )

    out = {
        "status": "ok",
        "total": len(itens),
        "criticos": len(criticos),
        "tarefas_criadas": tarefas_criadas,
        "cards_emitidos": cards_emitidos,
        "subpagina": subpage_id,
        "email_enviado": email_ok,
    }
    log.info("check-deadlines-legais", **out)
    return out
