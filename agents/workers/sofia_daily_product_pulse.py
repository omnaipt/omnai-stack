"""Worker: sofia-daily-product-pulse | Sofia | Product Owner | Sprint 6

Cron: Seg-Sex 07:50 (antes do briefing matinal das 08:18).

Le a database Backlog Produto no Notion. Para cada item, decide se e P0 aberto
(critico) ou Sprint sem numero atribuido (configuracao incompleta), e emite
cards no briefing. Gera tambem um card-resumo P3 silencioso.

Cards:
  - produto_p0_aberto (P0): item Prioridade=P0 e Status NOT IN (Done, Cancelled).
    Chave: ("produto_p0", page_id).
  - produto_sprint_incompleto (P1): Status=Sprint mas Sprint number vazio.
    Chave: ("produto_sprint_incompleto", page_id).
  - produto_pulse_diario (P2): card-resumo "N P0 abertos, M em sprint X, Y em backlog".
    Chave: ("produto_pulse", YYYYMMDD).

Schema Backlog Produto (legacy database_id, validado em runtime via fetch):
  - Hardcoded ID conforme briefing Marco. Se nao existe, worker faz log e sai
    sem rebentar.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx
import structlog

from services.briefing_emit import emit_briefing, page_url_from_id
from services.notion_ext import BASE, NOTION_TOKEN, NOTION_VERSION

log = structlog.get_logger()


WORKER_NAME = "sofia-daily-product-pulse"

# Database ID Backlog Produto. ID legacy (com hifens) conforme briefing Marco
# Sprint 6. Se a API rejeitar, fallback para data_source_id.
BACKLOG_PRODUTO_DB = "e21706c1-478a-4981-8d69-1eee41a5c5d2"

STATUS_FECHADOS = {"Done", "Cancelled", "Concluido", "Cancelado"}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _query_database(client: httpx.AsyncClient, db_id: str) -> list[dict]:
    """Faz query a database Notion. Pagina ate 200 items. Tenta legacy + data_source.

    Returns lista de pages (cada uma com properties + id).
    """
    items: list[dict] = []
    cursor: str | None = None

    for endpoint in (f"{BASE}/databases/{db_id}/query", f"{BASE}/data_sources/{db_id}/query"):
        items = []
        cursor = None
        while True:
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            try:
                r = await client.post(endpoint, json=body)
            except Exception as exc:
                log.warning("notion.query.exception", endpoint=endpoint, err=str(exc))
                break

            if r.status_code != 200:
                log.warning("notion.query.fail", endpoint=endpoint, status=r.status_code, body=r.text[:200])
                break

            data = r.json()
            items.extend(data.get("results", []))
            if not data.get("has_more"):
                return items
            cursor = data.get("next_cursor")
        if items:
            return items
    return items


def _prop_value(page: dict, name: str) -> dict | None:
    return page.get("properties", {}).get(name)


def _read_select(prop: dict | None) -> str | None:
    if not prop:
        return None
    p = prop.get("select") or prop.get("status")
    if isinstance(p, dict):
        return p.get("name")
    return None


def _read_number(prop: dict | None) -> int | None:
    if not prop:
        return None
    n = prop.get("number")
    return int(n) if isinstance(n, (int, float)) else None


def _read_title(page: dict) -> str:
    for name, prop in page.get("properties", {}).items():
        if prop.get("type") == "title":
            txt = "".join(t.get("plain_text", "") for t in prop.get("title", []))
            return txt.strip() or "(sem titulo)"
    return "(sem titulo)"


async def run() -> dict:
    today = date.today()
    today_iso = today.isoformat()

    pages: list[dict] = []
    async with httpx.AsyncClient(headers=_headers(), timeout=30.0) as client:
        pages = await _query_database(client, BACKLOG_PRODUTO_DB)

    if not pages:
        log.warning("backlog vazio ou inacessivel")
        await emit_briefing(
            tipo="produto_pulse_falha",
            titulo="Sofia: Backlog Produto inacessivel",
            detalhe=f"Notion query retornou 0 paginas. Verificar permissoes do integration token no DB {BACKLOG_PRODUTO_DB}.",
            urgencia="P1",
            empresa="OMNAI",
            chave_parts=("backlog_inacessivel", today_iso),
            metadata={"db_id": BACKLOG_PRODUTO_DB},
            worker_name=WORKER_NAME,
        )
        return {"status": "warn", "reason": "backlog vazio"}

    p0_abertos: list[dict] = []
    sprint_incompletos: list[dict] = []
    em_sprint: list[dict] = []
    em_backlog = 0
    em_done = 0

    for p in pages:
        prio = _read_select(_prop_value(p, "Prioridade")) or _read_select(_prop_value(p, "Priority"))
        status = _read_select(_prop_value(p, "Status"))
        sprint_n = _read_number(_prop_value(p, "Sprint"))
        titulo = _read_title(p)
        page_id = p.get("id", "")
        url = page_url_from_id(page_id)

        if status in STATUS_FECHADOS:
            em_done += 1
            continue

        if prio == "P0":
            p0_abertos.append({"id": page_id, "titulo": titulo, "status": status, "url": url})

        if status == "Sprint":
            em_sprint.append({"id": page_id, "titulo": titulo, "sprint": sprint_n, "url": url})
            if sprint_n is None:
                sprint_incompletos.append({"id": page_id, "titulo": titulo, "url": url})
        elif status in ("Backlog", "Idea", "Triage", None):
            em_backlog += 1

    # Cards individuais P0
    for it in p0_abertos:
        await emit_briefing(
            tipo="produto_p0_aberto",
            titulo=f"P0 produto aberto: {it['titulo'][:200]}",
            detalhe=f"Status actual: {it.get('status') or 'n/d'}. Sofia recomenda revisao prioritaria hoje.",
            urgencia="P0",
            empresa="OMNAI",
            chave_parts=(it["id"],),
            link_origem=it.get("url"),
            metadata={"page_id": it["id"], "status": it.get("status")},
            worker_name=WORKER_NAME,
        )

    # Cards individuais Sprint sem numero
    for it in sprint_incompletos:
        await emit_briefing(
            tipo="produto_sprint_incompleto",
            titulo=f"Sprint sem numero: {it['titulo'][:200]}",
            detalhe="Item esta em Status=Sprint mas o campo Sprint (number) esta vazio. Atribuir.",
            urgencia="P1",
            empresa="OMNAI",
            chave_parts=(it["id"],),
            link_origem=it.get("url"),
            metadata={"page_id": it["id"]},
            worker_name=WORKER_NAME,
        )

    # Sprint actual: maior numero distinto presente
    sprints_actuais = sorted({it["sprint"] for it in em_sprint if it["sprint"] is not None})
    sprint_atual = sprints_actuais[-1] if sprints_actuais else None

    # Card-resumo silencioso
    detalhe_resumo = (
        f"P0 abertos: {len(p0_abertos)} | Em sprint S{sprint_atual or '?'}: {len(em_sprint)} | "
        f"Backlog: {em_backlog} | Done historico: {em_done}"
    )
    await emit_briefing(
        tipo="produto_pulse_diario",
        titulo=f"Sofia pulse {today_iso}: {len(p0_abertos)} P0 | {len(em_sprint)} sprint | {em_backlog} backlog",
        detalhe=detalhe_resumo,
        urgencia="P2",
        empresa="OMNAI",
        chave_parts=("pulse", today_iso),
        metadata={
            "p0_count": len(p0_abertos),
            "sprint_count": len(em_sprint),
            "backlog_count": em_backlog,
            "sprint_atual": sprint_atual,
            "sprint_incompletos_count": len(sprint_incompletos),
        },
        worker_name=WORKER_NAME,
    )

    out = {
        "status": "ok",
        "data": today_iso,
        "p0_abertos": len(p0_abertos),
        "sprint_incompletos": len(sprint_incompletos),
        "em_sprint": len(em_sprint),
        "em_backlog": em_backlog,
        "em_done": em_done,
        "sprint_atual": sprint_atual,
    }
    log.info("sofia-daily-product-pulse", **out)
    return out
