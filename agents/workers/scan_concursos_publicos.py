"""scan-concursos-publicos v1.3 - worker unificado.

Agrega concursos publicos relevantes para Previnsa (SCIE/seguranca) e JMSoares
(seguranca electronica/redes/telecom). Substitui os stubs scan-concursos-publicos
e jmsoares-concursos-seguranca.

Sprint 4.3:
  * Mantem 100% da logica anterior (4 scrapers, classificacao por keywords,
    dedupe por Referencia, mark-expired, listas de activos, email rico via
    Carlos com scorecard supressivo).
  * Acrescenta emissao de cards em briefing_items via services.briefing_emit:
      - 1 card por concurso novo (entra sempre P2; a escalacao para P0 por
        prazo e feita diariamente pelo briefing_inbox via briefing_db).
      - 1 card-resumo por execucao quando ha novos.

Fontes V1.2 (active):
  * dados.gov.pt - Dataset oficial IMPIC/BASE com todos os anuncios do ano
    (JSON publico, cobre simultaneamente DRE Serie II-L + BASE.gov.pt).
    ~10k anuncios/ano. Plataforma reportada = \"BASE\".
  * AcinGov - autarquias e entidades publicas (HTML directo).
  * Vortal via Playwright (V1.3 com bot-protection bypass best-effort).

Fontes SPA/protegidas (TODO V1.4):
  * Saphety - por investigar.
  * ComprasPublicas - por investigar.

Pipeline:
  1. scrape_all() paralelo -> list[ConcursoRaw]
  2. Classificar por keywords -> Previnsa / JMSoares / ambos / None
  3. Dedupe por Referencia
  4. Upsert Notion: reusa se existe, senao cria
  5. Listar activos: Status in {Novo,Em Analise,Proposta Submetida}
     E (Prazo>=hoje OR Prazo vazio). Exclui Status=Sem Interesse (blacklist).
  6. Envia emails separados via Carlos (opaidapetinga@gmail.com):
       Previnsa -> david.sardinha@previnsa.com
       JMSoares -> david.sardinha@jmsoares.pt
  7. Sprint 4.3: emite cards em briefing_items (1 por novo + 1 resumo).
  8. Activity Log + retorna dict com stats.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup

from services import notion_ext
from services.briefing_emit import emit_briefing, page_url_from_id
from services.email_notify import send_notification
from services.output_formatting import (
    Scorecard, ScorecardRow, activity_log_minimal, should_publish,
)

log = structlog.get_logger()

# ---------- Constantes ----------
DS_JMSOARES = "2ce3b9e7-3220-4cdf-a255-e18e252ac031"  # database_id (page id)
DS_PREVINSA = "320dcf96-2aba-4ce5-91fa-1b5b655b3b40"  # database_id (page id)
DS_ACTIVITY = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
CMD_CENTER_PAGE = "33b973b92387810db140c7d445f7de1f"

CARLOS_SENDER = "opaidapetinga@gmail.com"
EMAIL_PREVINSA = "david.sardinha@previnsa.com"
EMAIL_JMSOARES = "david.sardinha@jmsoares.pt"

WORKER_NAME = "scan-concursos-publicos"

# Keywords por empresa (matching lower-case + sem acentos no objecto)
KEYWORDS_PREVINSA = [
    # Detecção Incêndio
    "deteccao de incendio", "sistema de incendio", "sadi",
    # Extinção de incêndios
    "extincao de incendio", "extincao de incendios", "combate a incendio",
    "sistema de extincao",
    # Brigadas de bombeiros
    "brigada de bombeiro", "brigadas de bombeiros", "equipa de intervencao",
    # Medidas de auto-protecção
    "medida de autoproteccao", "medidas de autoproteccao", "map ", "plano de seguranca",
    "auto-proteccao", "autoproteccao",
    # Protecção florestal
    "proteccao da floresta", "proteccao florestal", "dfci",
    "defesa da floresta", "defesa de floresta", "incendio rural", "incendio florestal",
    # Extintores
    "extintor", "extintores", "manutencao de extintores",
    # SCIE
    "scie", "seguranca contra incendio", "seguranca contra incendios",
]

KEYWORDS_JMSOARES = [
    # CCTV
    "cctv", "videovigilancia", "video-vigilancia", "camara de vigilancia",
    "camaras de vigilancia", "video vigilancia",
    # Controlo Acessos
    "controlo de acesso", "controlo de acessos", "controle de acesso",
    # Detecção de Intrusão
    "deteccao de intrusao", "sistema de intrusao", "alarme de intrusao",
    "anti-intrusao", "deteccao de intrusos",
    # Redes estruturadas
    "rede estruturada", "redes estruturadas", "cabecamento estruturado",
    "cablagem estruturada", "rede de dados",
    # Telecomunicações
    "telecomunicacao", "telecomunicacoes", "itur", "ited",
    "infraestrutura de telecomunicacoes",
]

STATUS_ACTIVOS = ("Novo", "Em Análise", "Proposta Submetida")

HTTP_TIMEOUT = 20.0
UA = "OMNAI-scan-concursos/1.3 (+https://agents.omnai.pt)"


@dataclass
class ConcursoRaw:
    referencia: str
    titulo: str
    entidade: str
    objecto: str
    valor_base: float | None
    plataforma: str
    data_pub: date | None
    prazo_propostas: date | None
    regiao: str
    url: str
    tipo_detectado: list[str] = field(default_factory=list)


# ---------- Utils ----------
def _norm(s: str) -> str:
    """Lowercase + strip acentos para matching."""
    if not s:
        return ""
    return (
        s.lower()
         .replace("á", "a").replace("ã", "a").replace("â", "a").replace("à", "a")
         .replace("é", "e").replace("ê", "e")
         .replace("í", "i").replace("ï", "i")
         .replace("ó", "o").replace("ô", "o").replace("õ", "o").replace("ò", "o")
         .replace("ú", "u").replace("ü", "u")
         .replace("ç", "c")
    )


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    # tentar ISO
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        pass
    # tentar dd-mm-yyyy ou dd/mm/yyyy
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return date(y, mo, d)
        except Exception:
            return None
    return None


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    # Portuguese number format: 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


# ---------- Scrapers ----------
# dados.gov.pt - Contratos Publicos Portal BASE IMPIC - Anuncios (dataset oficial)
# ID dataset fixo: 66d72fbc58cd7a63dae28712
# Ano corrente actualizado ~diariamente. Cobre DRE II-L + BASE.
DADOS_GOV_DATASET_ID = "66d72fbc58cd7a63dae28712"


async def _dados_gov_latest_json_resource(client: httpx.AsyncClient, ano: int) -> str | None:
    """Descobre o resource id do ficheiro anuncios<ano>.json no dataset."""
    try:
        r = await client.get(
            f"https://dados.gov.pt/api/1/datasets/{DADOS_GOV_DATASET_ID}/",
            headers={"User-Agent": UA},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        # Procurar resource cujo titulo/URL contem o ano em questao
        alvo = f"anuncios{ano}.json"
        for res in payload.get("resources", []):
            url = (res.get("url") or "").lower()
            title = (res.get("title") or "").lower()
            if res.get("format") == "json" and (alvo in url or alvo in title):
                return res.get("id")
        # Fallback: qualquer JSON do ano, preferindo o mais pequeno (mais recente)
        jsons = [r for r in payload.get("resources", []) if r.get("format") == "json"]
        if jsons:
            jsons.sort(key=lambda r: r.get("filesize") or 0)
            return jsons[0].get("id")
    except Exception as exc:
        log.warning("dados_gov.resource_discover.err", err=str(exc))
    return None


def _parse_pt_date(s: str | None) -> date | None:
    """Parse DD/MM/YYYY usado pelo dataset BASE."""
    if not s:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return _parse_iso_date(s)
    d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except Exception:
        return None


async def _scrape_dados_gov(client: httpx.AsyncClient) -> list[ConcursoRaw]:
    """Dataset oficial BASE/IMPIC via dados.gov.pt.

    Cobre DRE Serie II-L + BASE num unico JSON por ano.
    Filtra: anuncios com PrazoPropostas ainda aberto (data_pub + dias >= hoje).
    """
    out: list[ConcursoRaw] = []
    ano = date.today().year
    rid = await _dados_gov_latest_json_resource(client, ano)
    if not rid:
        log.warning("dados_gov.no_resource", ano=ano)
        return out
    try:
        url = f"https://dados.gov.pt/api/1/datasets/r/{rid}"
        r = await client.get(url, headers={"User-Agent": UA}, timeout=60.0)
        if r.status_code >= 400:
            log.warning("dados_gov.http_err", status=r.status_code)
            return out
        data = r.json()
        if not isinstance(data, list):
            log.warning("dados_gov.bad_payload", type=type(data).__name__)
            return out
        hoje = date.today()
        for item in data:
            ref = str(item.get("nAnuncio") or "").strip()
            if not ref or "/" not in ref:
                continue
            desc = str(item.get("descricaoAnuncio") or "").strip()
            entidade = str(item.get("designacaoEntidade") or "").strip()
            anuncio_url = str(item.get("url") or "").strip()
            modelo = str(item.get("modeloAnuncio") or "").strip()
            preco = item.get("PrecoBase")
            try:
                preco_f = float(str(preco).replace(",", ".")) if preco else None
            except Exception:
                preco_f = None
            data_pub = _parse_pt_date(item.get("dataPublicacao"))
            prazo_dias = item.get("PrazoPropostas")
            prazo_prop: date | None = None
            if data_pub and isinstance(prazo_dias, (int, float)) and prazo_dias > 0:
                try:
                    prazo_prop = data_pub + timedelta(days=int(prazo_dias))
                except Exception:
                    prazo_prop = None
            # Filtro: manter apenas com prazo ainda aberto (ou sem prazo mas publicado nos ultimos 60 dias)
            if prazo_prop and prazo_prop < hoje:
                continue
            if not prazo_prop and data_pub:
                if (hoje - data_pub).days > 60:
                    continue
            objecto = desc or ref
            if modelo:
                objecto = f"[{modelo}] {objecto}"
            out.append(ConcursoRaw(
                referencia=ref,
                titulo=(desc[:250] or ref),
                entidade=entidade,
                objecto=objecto,
                valor_base=preco_f,
                plataforma="BASE",
                data_pub=data_pub,
                prazo_propostas=prazo_prop,
                regiao="",
                url=anuncio_url,
            ))
        log.info("scan-concursos.dados_gov.ok", count=len(out), total_raw=len(data))
    except Exception as exc:
        log.warning("scan-concursos.dados_gov.err", err=str(exc))
    return out


async def _scrape_dre(client: httpx.AsyncClient) -> list[ConcursoRaw]:
    """DRE Serie II Seccao L - Contratacao Publica (BeautifulSoup)."""
    out: list[ConcursoRaw] = []
    url = (
        "https://dre.pt/dre/pesquisa-avancada/-/search/normal"
        "?type=dr&perPage=50&sort=publicationDate%3Adesc"
        "&seriesSelect=II&sectionSelect=L"
    )
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            log.warning("dre.http_err", status=r.status_code)
            return out
        soup = BeautifulSoup(r.text, "lxml")
        # DRE: cada resultado e um <a class="title"> ou <article>. Tentar multiplos
        articles = soup.find_all("article") or soup.select("div.list-item, li.list-item")
        for art in articles[:50]:
            text_all = art.get_text(" ", strip=True)
            m_ref = re.search(r"(?:Procedimento|Anuncio|Anúncio)[^\d]*(\d{3,6}/\d{4})", text_all, re.I)
            if not m_ref:
                m_ref = re.search(r"(\d{4,6}/\d{4})", text_all)
            if not m_ref:
                continue
            titulo_tag = art.find(["h1","h2","h3","h4"]) or art.find("a", class_=re.compile("title"))
            titulo = (titulo_tag.get_text(" ", strip=True) if titulo_tag else text_all)[:250]
            link_tag = art.find("a", href=True)
            href = link_tag["href"] if link_tag else ""
            if href and not href.startswith("http"):
                href = "https://dre.pt" + href
            m_date = re.search(r"(\d{4}-\d{2}-\d{2})", text_all)
            out.append(ConcursoRaw(
                referencia=m_ref.group(1),
                titulo=titulo,
                entidade="",
                objecto=titulo,
                valor_base=None,
                plataforma="DRE",
                data_pub=_parse_iso_date(m_date.group(1)) if m_date else None,
                prazo_propostas=None,
                regiao="",
                url=href or url,
            ))
        log.info("scan-concursos.dre.ok", count=len(out))
    except Exception as exc:
        log.warning("scan-concursos.dre.err", err=str(exc))
    return out


async def _scrape_base(client: httpx.AsyncClient) -> list[ConcursoRaw]:
    """BASE.gov.pt - pesquisa de procedimentos (BeautifulSoup)."""
    out: list[ConcursoRaw] = []
    queries = [
        "extintor", "scie", "incendio", "videovigilancia",
        "controlo de acessos", "intrusao", "redes estruturadas",
        "telecomunicacoes", "brigada", "auto-proteccao",
    ]
    seen: set[str] = set()
    for q in queries:
        try:
            r = await client.get(
                "https://www.base.gov.pt/Base4/pt/resultados/",
                params={"tipo": "1", "texto": q},
                headers={"User-Agent": UA},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            # BASE renders results in table rows. Columns vary; scan any <tr> with a link.
            for tr in soup.find_all("tr"):
                a = tr.find("a", href=True)
                if not a:
                    continue
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                ref = tds[0].get_text(" ", strip=True)
                if not ref or "/" not in ref:
                    continue
                if ref in seen:
                    continue
                seen.add(ref)
                entidade = tds[1].get_text(" ", strip=True) if len(tds) > 1 else ""
                objecto = tds[2].get_text(" ", strip=True) if len(tds) > 2 else ""
                valor = _parse_money(tds[3].get_text(" ", strip=True)) if len(tds) > 3 else None
                href = a["href"]
                if href and not href.startswith("http"):
                    href = "https://www.base.gov.pt" + href
                out.append(ConcursoRaw(
                    referencia=ref,
                    titulo=objecto[:250] or ref,
                    entidade=entidade,
                    objecto=objecto,
                    valor_base=valor,
                    plataforma="BASE",
                    data_pub=None,
                    prazo_propostas=None,
                    regiao="",
                    url=href,
                ))
        except Exception as exc:
            log.warning("scan-concursos.base.err", q=q, err=str(exc))
    log.info("scan-concursos.base.ok", count=len(out))
    return out


async def _scrape_vortal_playwright() -> list[ConcursoRaw]:
    """Vortal via Playwright headless.

    Nota: Vortal tem bot-protection avancada (Akamai/Cloudflare) que devolve 403
    mesmo com Chromium real + UA browser. Um retry detecta isto e degrada
    silenciosamente. TODO V1.4: investigar endpoint JSON interno que a SPA
    consome (DevTools network) ou usar sessao autenticada.
    """
    out: list[ConcursoRaw] = []
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        log.warning("vortal.playwright_missing", err=str(exc))
        return out
    # playwright-stealth opcional - patch anti-detection
    try:
        from playwright_stealth import Stealth
        _stealth = Stealth()
    except ImportError:
        _stealth = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="pt-PT",
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            if _stealth is not None:
                try:
                    await _stealth.apply_stealth_async(page)
                    log.info("vortal.stealth_applied")
                except Exception as exc:
                    log.warning("vortal.stealth_fail", err=str(exc))
            resp = await page.goto(
                "https://community.vortal.biz/pt/fornecedores/oportunidades",
                timeout=30000, wait_until="networkidle",
            )
            # Detectar bot-protection 403
            if resp and resp.status >= 400:
                log.warning("vortal.blocked", status=resp.status)
                await browser.close()
                return out
            title = await page.title()
            if "403" in title or "forbidden" in title.lower():
                log.warning("vortal.blocked", title=title)
                await browser.close()
                return out
            # Tentar aceitar cookies silenciosamente
            try:
                await page.click("button:has-text('Aceitar')", timeout=3000)
            except Exception:
                pass
            # Esperar cards renderizarem
            await page.wait_for_timeout(2000)
            # Varios selectors candidatos (Vortal muda markup)
            selectors = [
                "article.opportunity-card",
                "div.opportunity-card",
                "div.procedure-card",
                "li.result-item",
                "div[class*='opportunity']",
                "div[class*='procedure']",
                "tr.opportunity-row",
            ]
            cards = []
            for sel in selectors:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    log.info("vortal.selector_match", selector=sel, count=len(found))
                    break
            if not cards:
                log.warning("vortal.no_cards_found")
            for card in cards[:60]:
                try:
                    text_all = (await card.inner_text()).strip()
                except Exception:
                    continue
                m_ref = re.search(r"(\d{4,6}/\d{4})", text_all)
                if not m_ref:
                    continue
                titulo_el = await card.query_selector("h2, h3, h4, a.title, span.title")
                if titulo_el:
                    titulo = (await titulo_el.inner_text()).strip()[:250]
                else:
                    titulo = text_all[:120]
                link_el = await card.query_selector("a[href]")
                href = ""
                if link_el:
                    href = (await link_el.get_attribute("href")) or ""
                    if href and not href.startswith("http"):
                        href = "https://community.vortal.biz" + href
                out.append(ConcursoRaw(
                    referencia=m_ref.group(1),
                    titulo=titulo,
                    entidade="",
                    objecto=titulo,
                    valor_base=None,
                    plataforma="Vortal",
                    data_pub=None,
                    prazo_propostas=None,
                    regiao="",
                    url=href,
                ))
            await browser.close()
        log.info("scan-concursos.vortal.ok", count=len(out))
    except Exception as exc:
        log.warning("scan-concursos.vortal.err", err=str(exc))
    return out


async def _scrape_acingov(client: httpx.AsyncClient) -> list[ConcursoRaw]:
    """AcinGov - portal de autarquias (Sintra, Cascais, Forca Aerea, etc).

    Endpoint publico: /acingovprod/2/zonaPublica/zona_publica_c/indexProcedimentos
    HTML com tabela: Nº Procedimento | Tipo | Objeto | Entidade | Estado.
    """
    out: list[ConcursoRaw] = []
    try:
        r = await client.get(
            "https://www.acingov.pt/acingovprod/2/zonaPublica/zona_publica_c/indexProcedimentos",
            headers={"User-Agent": UA},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            log.warning("acingov.http_err", status=r.status_code)
            return out
        soup = BeautifulSoup(r.text, "lxml")
        # Procurar a 1a tabela com colunas [Nº Procedimento, Tipo, Objeto, Entidade, Estado]
        for tbl in soup.find_all("table"):
            rows = tbl.find_all("tr")
            if len(rows) < 2:
                continue
            head_text = " ".join(
                h.get_text(" ", strip=True).lower()
                for h in rows[0].find_all(["th", "td"])
            )
            if "procedimento" not in head_text or "objeto" not in head_text:
                continue
            for tr in rows[1:]:
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                ref = tds[0].get_text(" ", strip=True)
                tipo_txt = tds[1].get_text(" ", strip=True)
                objecto = tds[2].get_text(" ", strip=True)
                entidade = tds[3].get_text(" ", strip=True)
                estado = tds[4].get_text(" ", strip=True)
                # So incluir concursos abertos ("A receber propostas" / "Aberto")
                if estado and "propostas" not in estado.lower() and "aberto" not in estado.lower():
                    continue
                a = tr.find("a", href=True)
                href = a["href"] if a else ""
                if href and not href.startswith("http"):
                    href = "https://www.acingov.pt" + href
                out.append(ConcursoRaw(
                    referencia=ref,
                    titulo=(objecto[:250] or ref),
                    entidade=entidade,
                    objecto=f"[{tipo_txt}] {objecto}",
                    valor_base=None,
                    plataforma="AcinGov",
                    data_pub=None,
                    prazo_propostas=None,
                    regiao="",
                    url=href,
                ))
            break  # so a primeira tabela valida
        log.info("scan-concursos.acingov.ok", count=len(out))
    except Exception as exc:
        log.warning("scan-concursos.acingov.err", err=str(exc))
    return out


async def _scrape_stub(plataforma: str) -> list[ConcursoRaw]:
    log.info("scan-concursos.stub", plataforma=plataforma)
    return []


async def _scrape_all() -> list[ConcursoRaw]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            _scrape_dados_gov(client),         # BASE + DRE Serie II-L unificado
            _scrape_acingov(client),           # autarquias
            _scrape_vortal_playwright(),       # V1.3: playwright bypass bot-protection
            _scrape_stub("Saphety"),           # TODO V1.4
            _scrape_stub("ComprasPublicas"),   # TODO V1.4
            return_exceptions=True,
        )
    flat: list[ConcursoRaw] = []
    for r in results:
        if isinstance(r, list):
            flat.extend(r)
    return flat


# ---------- Classificacao ----------
def _classify(c: ConcursoRaw) -> list[str]:
    texto_n = _norm(c.titulo + " " + c.objecto)
    empresas: list[str] = []
    if any(k in texto_n for k in KEYWORDS_PREVINSA):
        empresas.append("previnsa")
    if any(k in texto_n for k in KEYWORDS_JMSOARES):
        empresas.append("jmsoares")
    return empresas


def _detect_tipo(c: ConcursoRaw, empresa: str) -> list[str]:
    """Detecta opcoes de 'Tipo' (multi_select) consoante empresa.

    Os nomes tem de ser exactamente os que estao no schema Notion.
    """
    t = _norm(c.titulo + " " + c.objecto)
    tipos: list[str] = []
    if empresa == "previnsa":
        if "deteccao de incendio" in t or "sadi" in t or "sistema de incendio" in t:
            tipos.append("Detecção Incêndio")
        if "extincao de incendio" in t or "combate a incendio" in t or "sistema de extincao" in t:
            tipos.append("Extinção Incêndios")
        if "brigada de bombeiro" in t or "brigadas de bombeiros" in t:
            tipos.append("Brigadas Bombeiros")
        if "auto-proteccao" in t or "autoproteccao" in t or " map " in t:
            tipos.append("Medidas Auto-Protecção")
        if "proteccao da floresta" in t or "proteccao florestal" in t or "dfci" in t or "incendio rural" in t or "incendio florestal" in t or "defesa da floresta" in t:
            tipos.append("Protecção Florestal")
        if "extintor" in t:
            tipos.append("Extintores / SCIE")
        if "scie" in t or "seguranca contra incendio" in t:
            tipos.append("SCIE")
    elif empresa == "jmsoares":
        if "cctv" in t or "videovigilancia" in t or "video-vigilancia" in t or "camara de vigilancia" in t:
            tipos.append("CCTV/Videovigilância")
        if "controlo de acesso" in t:
            tipos.append("Controlo Acessos")
        if "deteccao de intrusao" in t or "anti-intrusao" in t or "sistema de intrusao" in t:
            tipos.append("Detecção de Intrusão")
        if "rede estruturada" in t or "redes estruturadas" in t or "cabecamento estruturado" in t or "cablagem estruturada" in t:
            tipos.append("Redes Estruturadas")
        if "telecomunicacao" in t or "itur" in t or "ited" in t:
            tipos.append("Telecomunicações")
    return tipos or (["Misto"] if empresa in ("previnsa", "jmsoares") else [])


# ---------- Notion upsert ----------
async def _existing_refs(ds_id: str) -> set[str]:
    rows = await notion_ext.query_database(data_source_id=ds_id, page_size=100)
    refs: set[str] = set()
    for r in rows:
        props = r.get("properties", {})
        ref_prop = props.get("Referência", {}).get("rich_text") or []
        if ref_prop:
            refs.add("".join(p.get("plain_text", "") for p in ref_prop).strip())
    return refs


async def _upsert_concurso(
    ds_id: str,
    c: ConcursoRaw,
    empresa: str,
    existing: set[str],
) -> tuple[str, str | None]:
    """Cria a pagina Notion se nao existir.

    Devolve (status, page_id) onde status in {"created","skipped"} e page_id
    e o uuid da pagina criada (None quando skipped/erro).
    """
    if c.referencia in existing:
        return ("skipped", None)

    tipos = _detect_tipo(c, empresa)
    props: dict[str, Any] = {
        "Referência": {"rich_text": [{"type": "text", "text": {"content": c.referencia}}]},
        "Entidade": {"rich_text": [{"type": "text", "text": {"content": (c.entidade or "-")[:200]}}]},
        "Objecto": {"rich_text": [{"type": "text", "text": {"content": c.objecto[:1500]}}]},
        "Plataforma": {"select": {"name": c.plataforma}},
        "Status": {"select": {"name": "Novo"}},
    }
    if c.url:
        props["URL"] = {"url": c.url}
    if c.valor_base:
        props["Valor Base"] = {"number": c.valor_base}
    if c.data_pub:
        props["Data Publicação"] = {"date": {"start": c.data_pub.isoformat()}}
    if c.prazo_propostas:
        props["Prazo Propostas"] = {"date": {"start": c.prazo_propostas.isoformat()}}
    if c.regiao:
        props["Região"] = {"rich_text": [{"type": "text", "text": {"content": c.regiao[:200]}}]}
    if tipos:
        props["Tipo"] = {"multi_select": [{"name": t} for t in tipos]}

    page_id = await notion_ext.create_database_row(
        data_source_id=ds_id,
        title=f"{c.referencia} | {c.titulo[:100]}",
        properties_extra=props,
    )
    if page_id:
        log.info("scan-concursos.upsert.ok", empresa=empresa, ref=c.referencia, page=page_id)
        return ("created", page_id)
    return ("skipped", None)


async def _mark_expired_candidates(ds_id: str) -> int:
    hoje = date.today().isoformat()
    rows = await notion_ext.query_database(
        data_source_id=ds_id,
        filter_={
            "and": [
                {"property": "Status", "select": {"does_not_equal": "Prazo Expirado"}},
                {"property": "Status", "select": {"does_not_equal": "Sem Interesse"}},
                {"property": "Status", "select": {"does_not_equal": "Adjudicado"}},
                {"property": "Prazo Propostas", "date": {"before": hoje}},
            ]
        },
        page_size=100,
    )
    log.info("scan-concursos.expired.candidates", count=len(rows), ds=ds_id)
    return len(rows)


async def _list_activos(ds_id: str) -> list[dict]:
    hoje = date.today().isoformat()
    return await notion_ext.query_database(
        data_source_id=ds_id,
        filter_={
            "and": [
                {"or": [{"property": "Status", "select": {"equals": s}} for s in STATUS_ACTIVOS]},
                {"or": [
                    {"property": "Prazo Propostas", "date": {"on_or_after": hoje}},
                    {"property": "Prazo Propostas", "date": {"is_empty": True}},
                ]},
            ]
        },
        page_size=100,
    )


# ---------- Email ----------
def _format_email_html(empresa: str, activos: list[dict], novos_refs: list[str]) -> str:
    titulo = f"Concursos Activos {empresa.upper()} - {date.today().isoformat()}"
    rows_html = []
    for row in activos[:50]:
        p = row.get("properties", {})
        ref = "".join(t.get("plain_text", "") for t in p.get("Referência", {}).get("rich_text", []) or [])
        obj = "".join(t.get("plain_text", "") for t in p.get("Objecto", {}).get("rich_text", []) or [])
        ent = "".join(t.get("plain_text", "") for t in p.get("Entidade", {}).get("rich_text", []) or [])
        plat = (p.get("Plataforma", {}).get("select") or {}).get("name", "")
        prazo = ((p.get("Prazo Propostas", {}).get("date") or {}) or {}).get("start", "")
        valor = p.get("Valor Base", {}).get("number")
        url = p.get("URL", {}).get("url", "") or ""
        new_tag = " <strong>NOVO</strong>" if ref in novos_refs else ""
        rows_html.append(
            f"<tr><td>{ref}{new_tag}</td><td>{ent}</td><td>{obj[:200]}</td>"
            f"<td>{prazo or '-'}</td><td>{'€'+format(valor,',.0f') if valor else '-'}</td>"
            f"<td>{plat}</td><td><a href='{url}'>link</a></td></tr>"
        )
    table = (
        "<table border='1' cellpadding='6' style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px'>"
        "<tr><th>Ref</th><th>Entidade</th><th>Objecto</th><th>Prazo</th><th>Valor</th><th>Plat.</th><th>Link</th></tr>"
        + "".join(rows_html) + "</table>"
    )
    return (
        f"<h2>{titulo}</h2>"
        f"<p>{len(activos)} activos | {len(novos_refs)} novos desde ontem.</p>"
        f"<p><em>Marca Status=Sem Interesse na DB Notion para remover da pr&oacute;xima lista.</em></p>"
        + table
    )


async def _send_email(empresa: str, to_addr: str, activos: list[dict], novos_refs: list[str]) -> bool:
    if not activos and not novos_refs:
        log.info("scan-concursos.email.skip_empty", empresa=empresa)
        return False
    subject = f"Concursos {empresa.capitalize()} - {len(activos)} activos ({len(novos_refs)} novos)"
    html = _format_email_html(empresa, activos, novos_refs)
    res = await send_notification(
        subject=subject,
        html_body=html,
        to=to_addr,
        account=CARLOS_SENDER,
    )
    ok = bool(res)
    log.info("scan-concursos.email", empresa=empresa, to=to_addr, ok=ok)
    return ok


# ---------- Briefing emit (Sprint 4.3) ----------
def _empresa_canonica(empresa_slug: str) -> str:
    if empresa_slug == "previnsa":
        return "Previnsa"
    if empresa_slug == "jmsoares":
        return "JMSoares"
    return "OMNAI"


def _urgencia_pelo_prazo(prazo: date | None) -> str:
    """Urgencia de ENTRADA de um card de concurso: sempre P2.

    Modelo de escalacao por prazo (substitui o antigo P0/P1/P2 a entrada):
    todos os concursos entram em P2 e o briefing_inbox escala diariamente
    para P0, via briefing_db.escalate_concursos_by_prazo, os que continuam
    abertos com prazo a <= 5 dias. Concursos com prazo passado sao
    auto-dispensados por briefing_db.expire_overdue_concursos.

    Razao: o dataset dados.gov entrega concursos frequentemente ja perto do
    prazo, o que fazia quase tudo entrar P0 e esvaziava o significado do
    separador URGENTE. Fontes sem prazo (AcinGov, Vortal, DRE) ficam em P2
    ate haver prazo no metadata.
    """
    return "P2"


async def _emitir_card_concurso(
    c: ConcursoRaw,
    empresa_slug: str,
    notion_page_id: str | None,
) -> None:
    empresa = _empresa_canonica(empresa_slug)
    urgencia = _urgencia_pelo_prazo(c.prazo_propostas)

    valor_str = f"{c.valor_base:,.0f} EUR" if c.valor_base else "valor n/d"
    prazo_str = c.prazo_propostas.isoformat() if c.prazo_propostas else "sem prazo"

    titulo = f"Concurso {c.referencia}: {c.titulo[:140]}"
    detalhe_lines = [
        f"Entidade: {c.entidade or 'n/d'}.",
        f"Plataforma: {c.plataforma}.",
        f"Prazo: {prazo_str} | Valor base: {valor_str}.",
        f"Objecto: {c.objecto[:600]}",
    ]

    # link_origem privilegia a pagina Notion criada (David age a partir dali);
    # cai para o URL original se a pagina nao foi criada.
    link_origem = page_url_from_id(notion_page_id) or c.url or None

    await emit_briefing(
        tipo="concurso_novo",
        titulo=titulo,
        detalhe=" ".join(detalhe_lines),
        urgencia=urgencia,
        empresa=empresa,
        chave_parts=(empresa_slug, c.referencia),
        link_origem=link_origem,
        metadata={
            "referencia": c.referencia,
            "plataforma": c.plataforma,
            "entidade": c.entidade,
            "valor_base": c.valor_base,
            "prazo_propostas": prazo_str,
            "data_publicacao": c.data_pub.isoformat() if c.data_pub else None,
            "url_original": c.url,
            "notion_page_id": notion_page_id,
        },
        worker_name=WORKER_NAME,
    )


async def _emitir_resumo_scan(
    novos_prev: list[ConcursoRaw],
    novos_jms: list[ConcursoRaw],
) -> int:
    """Card-resumo unico por execucao quando ha novos. Devolve 0/1."""
    if not novos_prev and not novos_jms:
        return 0

    valor_p = sum((c.valor_base or 0.0) for c in novos_prev)
    valor_j = sum((c.valor_base or 0.0) for c in novos_jms)

    titulo = (
        f"Scan concursos: {len(novos_prev)} novos Previnsa "
        f"({valor_p:,.0f} EUR), {len(novos_jms)} novos JMSoares "
        f"({valor_j:,.0f} EUR)"
    )
    detalhe = (
        f"Previnsa: {len(novos_prev)} novos concursos para triagem. "
        f"JMSoares: {len(novos_jms)} novos concursos para triagem. "
        "Cards individuais emitidos em P2; escalacao diaria para P0 por prazo "
        "feita pelo briefing_inbox."
    )

    # Empresa do card-resumo: usa a que tem mais novos; em empate, OMNAI.
    if len(novos_prev) > len(novos_jms):
        empresa = "Previnsa"
    elif len(novos_jms) > len(novos_prev):
        empresa = "JMSoares"
    else:
        empresa = "OMNAI"

    await emit_briefing(
        tipo="scan_concursos_resumo",
        titulo=titulo,
        detalhe=detalhe,
        urgencia="P2",
        empresa=empresa,
        chave_parts=(date.today().isoformat(),),
        link_origem=None,
        metadata={
            "data": date.today().isoformat(),
            "previnsa_novos": len(novos_prev),
            "jmsoares_novos": len(novos_jms),
            "previnsa_valor_total": valor_p,
            "jmsoares_valor_total": valor_j,
        },
        worker_name=WORKER_NAME,
    )
    return 1


# ---------- Entry-point ----------
async def run() -> dict[str, Any]:
    raw = await _scrape_all()
    log.info("scan-concursos.scraped", total=len(raw))

    previnsa: list[ConcursoRaw] = []
    jmsoares: list[ConcursoRaw] = []
    for c in raw:
        empresas = _classify(c)
        if "previnsa" in empresas:
            previnsa.append(c)
        if "jmsoares" in empresas:
            jmsoares.append(c)

    existing_p = await _existing_refs(DS_PREVINSA)
    existing_j = await _existing_refs(DS_JMSOARES)
    novos_prev_refs: list[str] = []
    novos_jms_refs: list[str] = []
    novos_prev_full: list[ConcursoRaw] = []
    novos_jms_full: list[ConcursoRaw] = []

    cards_concursos = 0

    for c in previnsa:
        status, page_id = await _upsert_concurso(DS_PREVINSA, c, "previnsa", existing_p)
        if status == "created":
            novos_prev_refs.append(c.referencia)
            novos_prev_full.append(c)
            await _emitir_card_concurso(c, "previnsa", page_id)
            cards_concursos += 1
    for c in jmsoares:
        status, page_id = await _upsert_concurso(DS_JMSOARES, c, "jmsoares", existing_j)
        if status == "created":
            novos_jms_refs.append(c.referencia)
            novos_jms_full.append(c)
            await _emitir_card_concurso(c, "jmsoares", page_id)
            cards_concursos += 1

    cards_resumo = await _emitir_resumo_scan(novos_prev_full, novos_jms_full)

    exp_p = await _mark_expired_candidates(DS_PREVINSA)
    exp_j = await _mark_expired_candidates(DS_JMSOARES)
    activos_p = await _list_activos(DS_PREVINSA)
    activos_j = await _list_activos(DS_JMSOARES)

    # Fase 2: scorecard + suprimir-quando-vazio para decidir email vs activity log only.
    sc = Scorecard()
    if novos_prev_refs:
        sc.atencao.append(ScorecardRow(f"{len(novos_prev_refs)} novos concursos Previnsa para triagem"))
    if novos_jms_refs:
        sc.atencao.append(ScorecardRow(f"{len(novos_jms_refs)} novos concursos JMSoares para triagem"))
    if exp_p or exp_j:
        sc.automatizado.append(ScorecardRow(f"{exp_p + exp_j} concursos expirados marcados"))
    if activos_p or activos_j:
        sc.automatizado.append(ScorecardRow(f"{len(activos_p)}+{len(activos_j)} activos (Previnsa+JMSoares)"))

    sent_p = False
    sent_j = False
    if should_publish(sc):
        # Ha novidades ou falhas: enviar emails ricos
        sent_p = await _send_email("previnsa", EMAIL_PREVINSA, activos_p, novos_prev_refs)
        sent_j = await _send_email("jmsoares", EMAIL_JMSOARES, activos_j, novos_jms_refs)
        await notion_ext.create_database_row(
            data_source_id=DS_ACTIVITY,
            title=(
                f"Concursos {date.today().isoformat()} | "
                f"{len(novos_prev_refs)}+{len(novos_jms_refs)} novos | "
                f"{len(activos_p)}+{len(activos_j)} activos | "
                f"{cards_concursos}+{cards_resumo} cards briefing"
            ),
        )
    else:
        # Nenhuma novidade - registo curto sem spam de email
        await activity_log_minimal(
            "scan-concursos-publicos",
            f"rotina ok | {len(activos_p)}+{len(activos_j)} activos | 0 novos",
        )
        log.info("scan-concursos.skipped_email_empty")

    out = {
        "status": "ok",
        "scraped": len(raw),
        "classificados_previnsa": len(previnsa),
        "classificados_jmsoares": len(jmsoares),
        "previnsa": {
            "novos": len(novos_prev_refs),
            "activos": len(activos_p),
            "expirados_pend": exp_p,
            "email_enviado": sent_p,
        },
        "jmsoares": {
            "novos": len(novos_jms_refs),
            "activos": len(activos_j),
            "expirados_pend": exp_j,
            "email_enviado": sent_j,
        },
        "briefing_cards_concursos": cards_concursos,
        "briefing_cards_resumo": cards_resumo,
    }
    log.info("scan-concursos-publicos.done", **out)
    return out
