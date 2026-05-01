"""Worker: analise-concorrencia-semanal | Rita | Marketing

Cron: Segundas 09:01 UTC.

Sprint 5: emite cards em briefing_items por:
  * 1 `insight_concorrencia` P2 por concorrente com paginas alteradas
    (chave estavel competitor_slug + ano-semana ISO)
  * 1 `concorrencia_change_critical` P1 por pagina cujo URL/preview
    contenha sinais de pricing/launch (chave competitor_slug + change_id)
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import date

import httpx
import structlog

from services import notion_ext
from services.briefing_emit import emit_briefing, page_url_from_id
from services.competitors import todos
from services.llm import generate
from services.notion_ext import bullet, heading, paragraph
from services.state_ext import get_content_hash, set_content_hash

log = structlog.get_logger()


INTEL_COMP_DB = "7b758ae0-eb02-4601-b7bf-2850f4613a16"
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"

WORKER_NAME = "analise-concorrencia-semanal"

USER_AGENT = "OMNAIAgentTeam/1.0 (+https://omnai.pt)"
TIMEOUT = 10.0

# Sinais que indicam mudanca critica (pricing / launch / produto novo).
# Usados sobre a URL e sobre o resumo capturado, em lower-case.
CRITICAL_SIGNALS_URL = (
    "preco", "precos", "pricing", "tarifa", "tarifas", "planos",
    "plans", "launch", "lancamento", "novidade",
)
CRITICAL_SIGNALS_TEXT = (
    "novo preco", "preco actualizado", "novo plano", "lancamos",
    "launching", "agora disponivel", "now available", "new pricing",
    "price update", "novo produto", "novo servico",
)

EMPRESAS_CANONICAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")

SYSTEM_RITA = (
    "Es a Rita, Marketing Lead da OMNAI. Monitoria competitiva semanal. "
    "Portugues europeu, tu directo, sem cliches, sem travessoes. Maximo 350 palavras."
)


@dataclass
class PageCheck:
    empresa: str
    concorrente_id: str
    concorrente_nome: str
    url: str
    status: str  # unchanged | new | changed | error
    resumo: str = ""
    erro: str = ""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _strip_html(html: str) -> str:
    from html.parser import HTMLParser

    class Strip(HTMLParser):
        def __init__(self):
            super().__init__()
            self.chunks: list[str] = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                s = data.strip()
                if s:
                    self.chunks.append(s)

    p = Strip()
    p.feed(html)
    return "\n".join(p.chunks)


def _normalizar_empresa(raw: str) -> str:
    s = (raw or "").strip()
    return s if s in EMPRESAS_CANONICAS else "OMNAI"


def _is_critical(url: str, resumo: str) -> bool:
    u = (url or "").lower()
    if any(sig in u for sig in CRITICAL_SIGNALS_URL):
        return True
    r = (resumo or "").lower()
    if any(sig in r for sig in CRITICAL_SIGNALS_TEXT):
        return True
    return False


async def _verificar(empresa: str, conc: dict) -> list[PageCheck]:
    out: list[PageCheck] = []
    base = conc["website"].rstrip("/")
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for path in (conc["paginas_chave"] or ["/"]):
            url = base + path
            try:
                r = await client.get(url)
                r.raise_for_status()
                text = _strip_html(r.text)[:50_000]
            except Exception as exc:
                out.append(PageCheck(
                    empresa=empresa, concorrente_id=conc["id"],
                    concorrente_nome=conc["nome"], url=url,
                    status="error", erro=str(exc)[:200],
                ))
                continue

            new_sha = _hash(text)
            old = await get_content_hash(conc["id"], path)
            if old == new_sha:
                out.append(PageCheck(
                    empresa=empresa, concorrente_id=conc["id"],
                    concorrente_nome=conc["nome"], url=url, status="unchanged",
                ))
                continue

            status = "new" if old is None else "changed"
            out.append(PageCheck(
                empresa=empresa, concorrente_id=conc["id"],
                concorrente_nome=conc["nome"], url=url,
                status=status, resumo=text[:2000],
            ))
            await set_content_hash(conc["id"], path, new_sha)
    return out


async def _claude_resumir(checks: list[PageCheck]) -> str:
    relevantes = [c for c in checks if c.status in ("new", "changed")]
    if not relevantes:
        return "Sem alteracoes detectadas nos sites monitorizados esta semana."

    lista = "\n".join(
        f"- [{c.empresa}] {c.concorrente_nome}: {c.url} | {c.status}\n"
        f"  Preview: {c.resumo[:400].strip()}"
        for c in relevantes[:15]
    )
    prompt = (
        f"Alteracoes em concorrentes:\n\n{lista}\n\n"
        "Briefing:\n"
        "1) Resumo em 2-3 frases\n"
        "2) Destaques por empresa OMNAI / Previnsa / JMSoares\n"
        "3) Accoes sugeridas\n"
    )
    try:
        return await generate(system=SYSTEM_RITA, prompt=prompt, max_tokens=1200)
    except Exception as exc:
        log.error("claude resumir FAIL", err=str(exc))
        return "Nao foi possivel gerar resumo automatico esta semana."


async def _emitir_cards_concorrencia(
    checks: list[PageCheck],
    subpage_id: str | None,
) -> tuple[int, int]:
    """Emite cards por concorrente com alteracoes e cards criticos por pagina.

    Returns (insights, criticos).
    """
    relevantes = [c for c in checks if c.status in ("new", "changed")]
    if not relevantes:
        return 0, 0

    ano, semana, _ = date.today().isocalendar()
    yw = f"{ano}-W{semana:02d}"
    subpage_url = page_url_from_id(subpage_id)

    # Agrupa por concorrente
    por_conc: dict[str, list[PageCheck]] = {}
    for c in relevantes:
        por_conc.setdefault(c.concorrente_id, []).append(c)

    n_insights = 0
    n_criticos = 0

    for conc_id, lista in por_conc.items():
        first = lista[0]
        empresa_card = _normalizar_empresa(first.empresa)
        urls = ", ".join(c.url for c in lista[:5])
        preview = next((c.resumo[:600] for c in lista if c.resumo), "(sem preview)")
        titulo = f"Concorrencia {first.concorrente_nome}: {len(lista)} pagina(s) alteradas"
        detalhe = (
            f"Concorrente: {first.concorrente_nome}\n"
            f"Empresa OMNAI relevante: {first.empresa}\n"
            f"URLs: {urls}\n"
            f"Preview: {preview}"
        )
        link = lista[0].url or subpage_url
        ok = await emit_briefing(
            tipo="insight_concorrencia",
            titulo=titulo[:180],
            detalhe=detalhe,
            urgencia="P2",
            empresa=empresa_card,
            chave_parts=(conc_id, yw),
            link_origem=link,
            metadata={
                "competitor_slug": conc_id,
                "competitor_name": first.concorrente_nome,
                "empresa_target": first.empresa,
                "year_week": yw,
                "n_paginas_alteradas": len(lista),
                "urls": [c.url for c in lista],
            },
            worker_name=WORKER_NAME,
        )
        if ok:
            n_insights += 1

        # Cards criticos: 1 por pagina onde detectamos sinais de pricing/launch
        for c in lista:
            if not _is_critical(c.url, c.resumo):
                continue
            change_id = _hash(c.url)[:16]
            titulo_c = f"[CRITICO] {c.concorrente_nome} | {c.url}"
            detalhe_c = (
                f"Mudanca relevante detectada: {c.status}\n"
                f"URL: {c.url}\n"
                f"Empresa OMNAI relevante: {c.empresa}\n"
                f"Preview: {c.resumo[:600] if c.resumo else '(sem preview)'}"
            )
            okc = await emit_briefing(
                tipo="concorrencia_change_critical",
                titulo=titulo_c[:180],
                detalhe=detalhe_c,
                urgencia="P1",
                empresa=empresa_card,
                chave_parts=(conc_id, change_id),
                link_origem=c.url,
                metadata={
                    "competitor_slug": conc_id,
                    "competitor_name": c.concorrente_nome,
                    "url": c.url,
                    "status": c.status,
                    "change_id": change_id,
                    "empresa_target": c.empresa,
                },
                worker_name=WORKER_NAME,
            )
            if okc:
                n_criticos += 1

    return n_insights, n_criticos


async def run() -> dict:
    checks: list[PageCheck] = []
    for empresa, conc in todos():
        try:
            checks.extend(await _verificar(empresa, conc))
        except Exception as exc:
            log.error("verificar concorrente FAIL", empresa=empresa, id=conc.get("id"), err=str(exc))

    briefing = await _claude_resumir(checks)

    blocks = [paragraph(briefing), heading(2, "Detalhe por pagina")]
    for c in sorted(checks, key=lambda x: (x.empresa, x.concorrente_nome)):
        blocks.append(bullet(f"[{c.status.upper()}] {c.empresa} | {c.concorrente_nome} | {c.url}", link=c.url))

    ano, semana, _ = date.today().isocalendar()
    subpage_id = await notion_ext.create_child_page(
        parent_page_id=MORNING_BRIEFING_PAGE,
        title=f"Inteligencia Competitiva S{semana}/{ano}",
        children=blocks,
    )

    relevantes = [c for c in checks if c.status in ("new", "changed")]
    for c in relevantes:
        await notion_ext.create_database_row(
            data_source_id=INTEL_COMP_DB,
            title=f"[{c.empresa}] {c.concorrente_nome} | {c.status} | {date.today().isoformat()}",
        )

    cards_insight, cards_criticos = await _emitir_cards_concorrencia(checks, subpage_id)

    await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=(
            f"Inteligencia Competitiva S{semana} | {len(checks)} paginas | "
            f"{len(relevantes)} alteracoes | {cards_insight}+{cards_criticos} cards"
        ),
    )

    out = {
        "status": "ok",
        "paginas_verificadas": len(checks),
        "com_alteracoes": len(relevantes),
        "cards_insight": cards_insight,
        "cards_criticos": cards_criticos,
        "subpagina": subpage_id,
    }
    log.info("analise-concorrencia-semanal", **out)
    return out
