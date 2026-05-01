"""Worker: pipeline-review-semanal | Tiago | Sales

Cron: Segundas 09:04 UTC.

Sprint 4.3:
  * Mantem 100% da logica anterior (sub-pagina Notion gerada por Claude
    com plano semanal por categoria HOT/STALE/SINGLE_THREADED).
  * Acrescenta emissao de cards no briefing_items via services.briefing_emit:
        - 1 card por deal stale (P1).
        - 1 card-resumo por execucao (P2).
  * Mantem o property-name resolver com fallback (Stage/Estado, Empresa OMNAI/
    Empresa, Valor/Amount, Ultimo contacto/Last contact, Contactos/Contacts).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import structlog

from services import notion_ext
from services.briefing_emit import emit_briefing, page_url_from_id
from services.llm import generate
from services.notion import NotionClient
from services.notion_ext import bullet, heading, paragraph

log = structlog.get_logger()


PIPELINE_DB = "6db8052e-4e52-4099-bff6-0b21c50b8396"
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"

WORKER_NAME = "pipeline-review-semanal"

STAGES_FECHADAS = {"Won", "Lost", "Closed Lost", "Closed Won", "Fechado", "Perdido", "Ganho"}
STAGES_HOT = {"Qualified", "Proposal", "Negotiation", "Qualificado", "Proposta", "Negociacao"}
DIAS_STALE = 14

EMPRESAS_VALIDAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")

SYSTEM_TIAGO = (
    "Es o Tiago, Sales da OMNAI. Revisao semanal do pipeline. "
    "Portugues europeu, tu directo, sem cliches, sem travessoes. Maximo 300 palavras."
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _normalizar_empresa(raw: str) -> str:
    """Mapeia o select Notion para o conjunto canonico de briefing_items."""
    if not raw:
        return "OMNAI"
    raw_lower = raw.lower()
    for canonica in EMPRESAS_VALIDAS:
        if canonica.lower() == raw_lower:
            return canonica
    if "previn" in raw_lower:
        return "Previnsa"
    if "jms" in raw_lower or "soares" in raw_lower:
        return "JMSoares"
    if "sopato" in raw_lower:
        return "Sopato"
    if "omnai" in raw_lower:
        return "OMNAI"
    return "OMNAI"


def _extrair(row: dict, hoje: date) -> dict | None:
    props = row.get("properties", {})
    tprop = next((v for v in props.values() if v.get("type") == "title"), None)
    titulo = "".join(t.get("plain_text", "") for t in (tprop or {}).get("title", []))
    if not titulo:
        return None

    stage = ""
    sp = props.get("Stage", {}) or props.get("Estado", {})
    if sp.get("type") == "select":
        stage = (sp.get("select") or {}).get("name", "")
    elif sp.get("type") == "status":
        stage = (sp.get("status") or {}).get("name", "")

    if stage in STAGES_FECHADAS:
        return None

    valor = 0.0
    vp = props.get("Valor", {}) or props.get("Amount", {})
    if vp.get("type") == "number":
        valor = vp.get("number") or 0.0

    empresa_raw = ""
    ep = props.get("Empresa OMNAI", {}) or props.get("Empresa", {})
    if ep.get("type") == "select":
        empresa_raw = (ep.get("select") or {}).get("name", "")

    ultimo = None
    uc = props.get("Ultimo contacto", {}) or props.get("Last contact", {})
    if uc.get("type") == "date":
        ultimo = _parse_date((uc.get("date") or {}).get("start"))
    if ultimo is None:
        ultimo = _parse_date(row.get("last_edited_time", ""))

    dias_sem = (hoje - ultimo).days if ultimo else None

    contactos = 0
    cp = props.get("Contactos", {}) or props.get("Contacts", {})
    if cp.get("type") == "relation":
        contactos = len(cp.get("relation", []))

    # Proxima accao (texto livre OU date). Usado para a heuristica stale_sem_accao
    # e stale_atrasado. Cobertura: tipos rich_text, title, date, formula(string|date).
    proxima_accao_texto = ""
    proxima_accao_data: date | None = None
    pa = (
        props.get("Proxima Accao", {})
        or props.get("Próxima Accao", {})
        or props.get("Próxima Acção", {})
        or props.get("Next action", {})
        or props.get("Next Action", {})
    )
    pa_type = pa.get("type")
    if pa_type == "rich_text":
        proxima_accao_texto = "".join(
            t.get("plain_text", "") for t in pa.get("rich_text", [])
        ).strip()
    elif pa_type == "title":
        proxima_accao_texto = "".join(
            t.get("plain_text", "") for t in pa.get("title", [])
        ).strip()
    elif pa_type == "date":
        proxima_accao_data = _parse_date((pa.get("date") or {}).get("start"))
    elif pa_type == "formula":
        f = pa.get("formula", {})
        if f.get("type") == "string":
            proxima_accao_texto = (f.get("string") or "").strip()
        elif f.get("type") == "date":
            proxima_accao_data = _parse_date((f.get("date") or {}).get("start"))

    pad = (
        props.get("Data Proxima Accao", {})
        or props.get("Data Próxima Acção", {})
        or props.get("Next action date", {})
    )
    if pad.get("type") == "date" and proxima_accao_data is None:
        proxima_accao_data = _parse_date((pad.get("date") or {}).get("start"))

    categorias: list[str] = []
    if stage in STAGES_HOT and dias_sem is not None and dias_sem <= 7:
        categorias.append("HOT")
    if dias_sem is not None and dias_sem >= DIAS_STALE:
        categorias.append("STALE")
    if contactos == 1:
        categorias.append("SINGLE_THREADED")

    # Heuristicas stale para emit_briefing (Sprint 4.3).
    stale_motivos: list[str] = []
    if not proxima_accao_texto and proxima_accao_data is None:
        stale_motivos.append("deal_stale_sem_accao")
    if proxima_accao_data and proxima_accao_data < hoje:
        stale_motivos.append("deal_stale_atrasado")
    if dias_sem is not None and dias_sem > DIAS_STALE:
        stale_motivos.append("deal_stale_silencioso")

    return {
        "row_id": row.get("id"),
        "url": row.get("url"),
        "titulo": titulo,
        "stage": stage,
        "valor": valor,
        "empresa_raw": empresa_raw,
        "empresa": _normalizar_empresa(empresa_raw),
        "dias_sem_contacto": dias_sem,
        "contactos_count": contactos,
        "categorias": categorias,
        "proxima_accao_texto": proxima_accao_texto,
        "proxima_accao_data": proxima_accao_data.isoformat() if proxima_accao_data else None,
        "stale_motivos": stale_motivos,
    }


async def _claude_plano(deals: list[dict]) -> str:
    def fmt(d: dict) -> str:
        return (
            f"{d['titulo']} | {d['stage']} | {d['empresa']} | "
            f"{d['valor']:.0f} EUR | {d['dias_sem_contacto']}d"
        )

    hot = [d for d in deals if "HOT" in d["categorias"]]
    stale = [d for d in deals if "STALE" in d["categorias"]]
    single = [d for d in deals if "SINGLE_THREADED" in d["categorias"]]

    prompt = (
        f"Total deals abertos: {len(deals)}\n"
        f"HOT ({len(hot)}): {'; '.join(fmt(d) for d in hot[:10])}\n"
        f"STALE ({len(stale)}): {'; '.join(fmt(d) for d in stale[:10])}\n"
        f"SINGLE_THREADED ({len(single)}): {'; '.join(fmt(d) for d in single[:10])}\n\n"
        "Plano da semana:\n1) Prioridades para fechar\n2) Risco imediato\n"
        "3) Follow-up dos STALE\n4) Desbloquear SINGLE_THREADED"
    )
    try:
        return await generate(system=SYSTEM_TIAGO, prompt=prompt, max_tokens=1000)
    except Exception as exc:
        log.error("claude plano FAIL", err=str(exc))
        return "Nao foi possivel gerar plano automatico esta semana."


async def _emitir_cards_stale(deals: list[dict], subpage_url: str | None) -> int:
    """Emite 1 card por deal stale. Devolve numero de cards emitidos."""
    emitidos = 0
    for d in deals:
        if not d["stale_motivos"]:
            continue
        # Determinar tipo dominante: atrasado > sem accao > silencioso
        if "deal_stale_atrasado" in d["stale_motivos"]:
            tipo_card = "deal_stale_atrasado"
            detalhe_extra = (
                f"Proxima accao prevista para {d['proxima_accao_data']} ja passou."
            )
        elif "deal_stale_sem_accao" in d["stale_motivos"]:
            tipo_card = "deal_stale_sem_accao"
            detalhe_extra = "Sem proxima accao definida."
        else:
            tipo_card = "deal_stale_silencioso"
            detalhe_extra = (
                f"Sem contacto ha {d['dias_sem_contacto']} dias."
            )

        valor_str = f"{d['valor']:,.0f} EUR" if d["valor"] else "valor n/d"
        titulo = (
            f"Deal parado: {d['titulo']} | {d['stage']} | {valor_str}"
        )
        detalhe_lines = [
            detalhe_extra,
            f"Empresa: {d['empresa']}.",
            f"Stage: {d['stage'] or '-'} | Contactos: {d['contactos_count']}.",
        ]
        if d["proxima_accao_texto"]:
            detalhe_lines.append(f"Proxima accao: {d['proxima_accao_texto']}")
        if subpage_url:
            detalhe_lines.append(f"Plano semanal: {subpage_url}")

        # link_origem privilegia o URL do deal; se ausente, cai para a sub-pagina semanal.
        link_origem = d.get("url") or subpage_url

        await emit_briefing(
            tipo=tipo_card,
            titulo=titulo,
            detalhe=" ".join(detalhe_lines),
            urgencia="P1",
            empresa=d["empresa"],
            chave_parts=(d["row_id"] or d["titulo"],),
            link_origem=link_origem,
            metadata={
                "deal_id": d["row_id"],
                "stage": d["stage"],
                "valor": d["valor"],
                "dias_sem_contacto": d["dias_sem_contacto"],
                "categorias": d["categorias"],
                "stale_motivos": d["stale_motivos"],
            },
            worker_name=WORKER_NAME,
        )
        emitidos += 1
    return emitidos


async def _emitir_resumo_por_empresa(
    deals: list[dict],
    semana: int,
    ano: int,
    subpage_url: str | None,
) -> int:
    """1 card-resumo por empresa com deals abertos. Devolve numero de cards."""
    por_empresa: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        por_empresa[d["empresa"]].append(d)

    emitidos = 0
    for empresa, lista in por_empresa.items():
        n = len(lista)
        valor_total = sum(x["valor"] for x in lista)
        titulo = (
            f"Pipeline review S{semana}/{ano}: {n} deals abertos "
            f"({valor_total:,.0f} EUR)"
        )
        hot_n = sum(1 for x in lista if "HOT" in x["categorias"])
        stale_n = sum(1 for x in lista if "STALE" in x["categorias"])
        single_n = sum(1 for x in lista if "SINGLE_THREADED" in x["categorias"])
        detalhe = (
            f"{empresa}: {n} deals abertos. HOT: {hot_n} | STALE: {stale_n} "
            f"| SINGLE_THREADED: {single_n}. Valor acumulado {valor_total:,.0f} EUR."
        )
        await emit_briefing(
            tipo="pipeline_review_semanal",
            titulo=titulo,
            detalhe=detalhe,
            urgencia="P2",
            empresa=empresa,
            chave_parts=(empresa, f"{ano}-W{semana:02d}"),
            link_origem=subpage_url,
            metadata={
                "ano": ano,
                "semana": semana,
                "deals_abertos": n,
                "valor_total_eur": valor_total,
                "hot": hot_n,
                "stale": stale_n,
                "single_threaded": single_n,
            },
            worker_name=WORKER_NAME,
        )
        emitidos += 1
    return emitidos


async def run() -> dict[str, Any]:
    hoje = date.today()
    ano, semana, _ = hoje.isocalendar()

    async with NotionClient() as nc:
        try:
            resp = await nc.query_data_source(data_source_id=PIPELINE_DB, page_size=100)
            rows = resp.get("results", [])
        except Exception as exc:
            log.error("query Pipeline FAIL", err=str(exc))
            rows = []

    deals = [d for r in rows if (d := _extrair(r, hoje)) is not None]

    if not deals:
        await notion_ext.create_database_row(
            data_source_id=ACTIVITY_LOG_DB,
            title=f"pipeline-review-semanal | {hoje.isoformat()} | pipeline vazio",
        )
        return {"status": "ok", "deals": 0, "nota": "pipeline vazio"}

    plano = await _claude_plano(deals)

    blocks = [
        paragraph(
            f"Pipeline aberto: {len(deals)} deals, "
            f"{sum(d['valor'] for d in deals):,.0f} EUR acumulados."
        ),
        heading(2, "Plano da semana"),
        paragraph(plano),
        heading(2, "Deals por categoria"),
    ]

    for cat in ("HOT", "STALE", "SINGLE_THREADED"):
        items = [d for d in deals if cat in d["categorias"]]
        if not items:
            continue
        blocks.append(heading(3, f"{cat} ({len(items)})"))
        for d in items[:15]:
            label = (
                f"{d['titulo']} | {d['stage']} | {d['empresa']} | "
                f"{d['valor']:.0f} EUR | {d.get('dias_sem_contacto', '?')}d"
            )
            blocks.append(bullet(label, link=d.get("url")))

    subpage_id = await notion_ext.create_child_page(
        parent_page_id=MORNING_BRIEFING_PAGE,
        title=f"Pipeline Review S{semana}/{ano}",
        children=blocks,
    )
    subpage_url = page_url_from_id(subpage_id)

    hot = sum(1 for d in deals if "HOT" in d["categorias"])
    stale = sum(1 for d in deals if "STALE" in d["categorias"])

    # Sprint 4.3: emitir cards em briefing_items.
    cards_stale = await _emitir_cards_stale(deals, subpage_url)
    cards_resumo = await _emitir_resumo_por_empresa(deals, semana, ano, subpage_url)

    await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=(
            f"pipeline-review-semanal | {len(deals)} deals | {hot} HOT | "
            f"{stale} STALE | {cards_stale}+{cards_resumo} cards briefing"
        ),
    )

    out: dict[str, Any] = {
        "status": "ok",
        "deals_abertos": len(deals),
        "hot": hot,
        "stale": stale,
        "subpagina": subpage_id,
        "briefing_cards_stale": cards_stale,
        "briefing_cards_resumo": cards_resumo,
    }
    log.info("pipeline-review-semanal", **out)
    return out
