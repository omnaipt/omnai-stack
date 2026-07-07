"""Worker: briefing-inbox v9.4.0 (Sprint 7.6)

Mantem todas as features da v9.3.0:
- Sincronizacao Notion to-do checked -> DB (mark_done_by_chave)
- Cards URGENTE / ESTA SEMANA / ACOMPANHAR / BAIXA agrupados por urgencia
- Resumo Carlos no topo
- Seccao 'Resolvido nas ultimas 24h' no fundo
- 5 seccoes Sprint 7: Proximos 7 dias, Drafts, Resumo emails, Faturas, Resolvido

Mudanca chave Sprint 7.6:
- A pagina \U0001f305 Hoje passa a ter uma area STATICA (preservada entre execucoes)
  com a vista linked database 'Tarefas Hoje (David)' interactiva, e uma area
  DINAMICA delimitada por markers HTML que e regenerada a cada execucao.
- A funcao replace_page_content foi substituida por regenerate_dynamic_section,
  que apaga apenas os blocks entre <!--BRIEFING_AGENT_START--> e
  <!--BRIEFING_AGENT_END--> e re-adiciona o conteudo dinamico no fim.
- O callout link 'To-do (David)' do Sprint 7 e REMOVIDO; a vista linked
  database persistente no topo da pagina substitui-o por interactividade
  directa (David clica '+ Nova' na vista para criar tarefas).
- Compatibilidade backwards: se os markers nao existirem na pagina, faz
  fallback para replace_page_content (comportamento Sprint 7) e adiciona os
  markers no fim para proximas execucoes funcionarem em modo incremental.

Setup inicial: correr setup_todo_view.py UMA vez para configurar a vista
persistente + markers. Depois o briefing_inbox encarrega-se do resto.

Briefing accionavel (2026-07):
- Antes do list_open: briefing_db.expire_overdue_concursos (auto-dismiss de
  concursos com prazo passado) e briefing_db.escalate_concursos_by_prazo
  (P2 -> P0 quando faltam <= 5 dias). Ordem importa: expirar primeiro.
- A entrada do Activity Log passa a ter children: top 10 itens P0/P1 com
  empresa, prazo e accao proposta, mais o resumo do Carlos. O titulo inclui
  expirados/escalados/resolvidos.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog

from services import briefing_db, notion_ext
from services.action_tokens import link_for, make_token
from services.briefing_db import mark_done_by_chave
from services.llm import generate
from services.notion import NotionClient
from services.notion_ext import bullet, heading, paragraph, rt
from services.state import get_all_email_stats, peek_drafts, peek_invoices

log = structlog.get_logger()


MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
TAREFAS_DATA_SOURCE = "119b5aae-583a-4646-b732-a0975f7cf4bd"
ACTIONS_BASE_URL = os.getenv("ACTIONS_BASE_URL", "https://agents.omnai.pt")

# Markers HTML que delimitam a area dinamica regenerada a cada execucao.
# Tudo o que estiver ANTES de START e' preservado (vista linked database, etc).
MARKER_START = "<!-- BRIEFING_AGENT_START -->"
MARKER_END = "<!-- BRIEFING_AGENT_END -->"


URGENCIA_MAP = {
    "P0": ("\U0001f534", "URGENTE"),
    "P1": ("\U0001f7e1", "ESTA SEMANA"),
    "P2": ("\U0001f7e2", "ACOMPANHAR"),
    "P3": ("⚪", "BAIXA PRIORIDADE"),
}

EMPRESA_COLORS = {
    "OMNAI": "blue",
    "Previnsa": "green",
    "JMSoares": "orange",
    "Sopato": "purple",
    "Pessoal": "gray",
}

# Tipos de cards que tem prazos relevantes para a seccao "Proximos 7 dias"
DEADLINE_TIPOS = (
    "deadline_legal",
    "deadline_contabilidade",
    "concurso_novo",
    "fecho_contabilistico",
    "extracto_bancario",
)

# Inboxes que aparecem no resumo arquivo emails (ordem fixa)
INBOX_ORDER = [
    "david.sardinha@omnai.pt",
    "hello@omnai.pt",
    "opaidapetinga@gmail.com",
    "sopato.cascais@gmail.com",
    "david.sardinha@sapo.pt",
    "davidsardinhalves@gmail.com",
    "david.sardinha@jmsoares.pt",
]

SYSTEM_CARLOS = (
    "Es o Carlos, Chief of Staff do David. Resume em UM unico paragrafo curto "
    "(maximo 3 frases) o estado do inbox executivo. Portugues europeu, tu directo, "
    "sem cliches, sem travessoes. NAO inventes tarefas. NAO faces listas. "
    "Foca-te no que e mais critico hoje e no que pode esperar."
)


CHAVE_MARKER = re.compile(r"<!--briefing-key:([0-9a-f]{64})-->")


# ----------------------------------------------------------------------
# Sync Notion to-do -> DB (mantido inalterado da v9.2.1)
# ----------------------------------------------------------------------

async def _sync_notion_to_db(nc: NotionClient) -> int:
    try:
        blocks = await nc.get_block_children(MORNING_BRIEFING_PAGE)
    except Exception as exc:
        log.warning("sync_notion_to_db FAIL", err=str(exc))
        return 0

    marcados = 0
    for b in blocks:
        if b.get("type") != "to_do":
            continue
        td = b.get("to_do", {})
        if not td.get("checked"):
            continue
        rtxt = "".join(t.get("plain_text", "") for t in td.get("rich_text", []))
        m = CHAVE_MARKER.search(rtxt)
        if not m:
            continue
        chave = m.group(1)
        try:
            if await mark_done_by_chave(chave):
                marcados += 1
        except Exception as exc:
            log.warning("mark_done_by_chave FAIL", chave=chave[:12], err=str(exc))

    return marcados


# ----------------------------------------------------------------------
# Cards URGENTE / ESTA SEMANA / ACOMPANHAR (mantidos da v9.2.1)
# ----------------------------------------------------------------------

def _empresa_tag(empresa: str | None) -> str:
    if not empresa:
        return ""
    return f"  ·  {empresa}"


def _build_card(item: dict) -> list[dict]:
    """Card aberto: callout colorido com 3 links accao."""
    item_id = str(item["id"])
    chave = item["chave"]
    emoji, _ = URGENCIA_MAP.get(item.get("urgencia", "P2"), ("⚪", ""))

    titulo = item.get("titulo") or ""
    detalhe = item.get("detalhe") or ""
    empresa = item.get("empresa")

    title_parts = [
        {"type": "text", "text": {"content": titulo}, "annotations": {"bold": True}},
    ]
    if empresa:
        title_parts.append({
            "type": "text",
            "text": {"content": _empresa_tag(empresa)},
            "annotations": {"color": EMPRESA_COLORS.get(empresa, "default")},
        })
    title_parts.append({
        "type": "text",
        "text": {"content": f" <!--briefing-key:{chave}-->"},
        "annotations": {"color": "gray"},
    })

    callout_children: list[dict] = []
    if detalhe:
        callout_children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": detalhe}}],
            },
        })

    link_done = link_for(ACTIONS_BASE_URL, item_id, "done")
    link_snooze = link_for(ACTIONS_BASE_URL, item_id, "snooze", days=7)
    link_dismiss = link_for(ACTIONS_BASE_URL, item_id, "dismiss")

    actions_rt: list[dict] = [
        {"type": "text", "text": {"content": "✓ Resolver", "link": {"url": link_done}}, "annotations": {"color": "green"}},
        {"type": "text", "text": {"content": "    "}},
        {"type": "text", "text": {"content": "⏰ Snooze 7d", "link": {"url": link_snooze}}, "annotations": {"color": "yellow"}},
        {"type": "text", "text": {"content": "    "}},
        {"type": "text", "text": {"content": "\U0001f5d1 Dispensar", "link": {"url": link_dismiss}}, "annotations": {"color": "gray"}},
    ]
    if item.get("link_origem"):
        actions_rt = [
            {"type": "text", "text": {"content": "\U0001f517 Abrir", "link": {"url": item["link_origem"]}}, "annotations": {"color": "blue"}},
            {"type": "text", "text": {"content": "    "}},
        ] + actions_rt

    callout_children.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": actions_rt},
    })

    cor_callout = {
        "P0": "red_background",
        "P1": "yellow_background",
        "P2": "green_background",
        "P3": "gray_background",
    }.get(item.get("urgencia", "P2"), "default")

    callout = {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": emoji},
            "color": cor_callout,
            "rich_text": title_parts,
            "children": callout_children,
        },
    }
    return [callout]


def _build_resolved_card(item: dict) -> dict:
    """Card resolvido: callout cinza, sem links, com check."""
    titulo = item.get("titulo") or ""
    empresa = item.get("empresa") or ""
    resolvido = item.get("resolvido_em")
    status = item.get("status", "done")

    hora = ""
    if isinstance(resolvido, datetime):
        hora = resolvido.strftime("%H:%M")

    icon = "✅" if status == "done" else "\U0001f5d1️"
    label = "Resolvido" if status == "done" else "Dispensado"

    title_parts: list[dict] = [
        {"type": "text", "text": {"content": titulo}, "annotations": {"strikethrough": True, "color": "gray"}},
    ]
    sufix = f"  ·  {label}"
    if empresa:
        sufix += f"  ·  {empresa}"
    if hora:
        sufix += f"  ·  {hora}"
    title_parts.append({
        "type": "text",
        "text": {"content": sufix},
        "annotations": {"color": "gray"},
    })

    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": icon},
            "color": "gray_background",
            "rich_text": title_parts,
        },
    }


# ----------------------------------------------------------------------
# Sprint 7.6 - Regenerador area dinamica entre markers HTML
# ----------------------------------------------------------------------

def _block_plain_text(block: dict) -> str:
    """Extrai texto plano de um block (paragraph/heading/etc)."""
    btype = block.get("type")
    if not btype:
        return ""
    payload = block.get(btype) or {}
    rt_arr = payload.get("rich_text") or []
    parts = []
    for r in rt_arr:
        # API devolve plain_text directo
        pt = r.get("plain_text")
        if pt is not None:
            parts.append(pt)
            continue
        txt = (r.get("text") or {}).get("content")
        if txt:
            parts.append(txt)
    return "".join(parts)


def _find_marker_indices(blocks: list[dict]) -> tuple[int | None, int | None]:
    """Devolve (idx_start, idx_end) dos blocks que contem MARKER_START e
    MARKER_END. Caso nao existam, devolve (None, None) ou parciais.
    """
    idx_start: int | None = None
    idx_end: int | None = None
    for i, b in enumerate(blocks):
        text = _block_plain_text(b)
        if idx_start is None and MARKER_START in text:
            idx_start = i
            continue
        if MARKER_START in text and MARKER_END in text and idx_start is None:
            # Caso degenerado: ambos no mesmo block. Nao suportado, ignorar.
            continue
        if MARKER_END in text:
            idx_end = i
            # Nao break: queremos o ULTIMO END caso haja duplicados (defensivo).
    return idx_start, idx_end


def _marker_block(content: str) -> dict:
    """Block paragraph apenas com texto cinza contendo o marker HTML."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": content},
                "annotations": {"color": "gray", "italic": True},
            }],
        },
    }


async def regenerate_dynamic_section(
    nc: NotionClient, page_id: str, new_content: list[dict]
) -> dict[str, Any]:
    """Regenera apenas a area dinamica entre MARKER_START e MARKER_END.

    Comportamento:
    1. Le todos os blocks da pagina via get_block_children.
    2. Identifica indices dos markers.
    3. Se ambos existem: apaga blocks entre eles (exclusivo nas pontas) e
       tambem o block END (que sera re-adicionado no fim). Mantem START intacto.
       Depois faz append em sequencia: new_content + END marker.
       Nota: a Notion API so suporta append no FIM da lista de filhos. Para
       que isto resulte sem reordenar a vista linked database, o END marker
       deve ser o ULTIMO block da pagina antes da regeneracao. Como apagamos
       END junto com o conteudo dinamico, o append no fim coloca o novo
       conteudo + END novamente no fim, depois de START. Funciona desde que
       nao haja blocks STATICOS apos END (que e o desenho).
    4. Se markers nao existem: fallback compativel. Faz replace_page_content
       de forma especial: preserva blocks ate encontrar um heading
       'Briefing YYYY-...' (heuristica do conteudo dinamico antigo Sprint 7),
       senao apaga tudo. Depois adiciona markers no fim.

    Devolve dict com info para logging.
    """
    info: dict[str, Any] = {"mode": "unknown", "deleted": 0, "appended": 0}

    try:
        existing = await nc.get_block_children(page_id)
    except Exception as exc:
        log.error("get_block_children FAIL", err=str(exc))
        raise

    idx_start, idx_end = _find_marker_indices(existing)

    if idx_start is not None and idx_end is not None and idx_end > idx_start:
        # Modo incremental: apagar blocks entre START (exclusivo) e END (inclusivo).
        # END e re-adicionado no fim do append.
        info["mode"] = "incremental"
        to_delete = existing[idx_start + 1 : idx_end + 1]
        for b in to_delete:
            try:
                await nc.delete_block(b["id"])
                info["deleted"] += 1
            except Exception as exc:
                log.warning("delete_block FAIL", id=b.get("id"), err=str(exc))

        # Append: conteudo novo + END marker novamente
        payload = list(new_content) + [_marker_block(MARKER_END)]
        await nc.append_blocks(page_id, payload)
        info["appended"] = len(payload)
        return info

    # Modo fallback: markers ausentes ou parciais. Comportamento Sprint 7
    # (replace_page_content) com adicao dos markers no fim para proximas
    # execucoes operarem em modo incremental.
    info["mode"] = "fallback_replace"
    log.warning(
        "markers ausentes na pagina, fallback replace_page_content",
        idx_start=idx_start,
        idx_end=idx_end,
    )

    for b in existing:
        try:
            await nc.delete_block(b["id"])
            info["deleted"] += 1
        except Exception as exc:
            log.warning("delete_block FAIL (fallback)", id=b.get("id"), err=str(exc))

    # No fallback NAO ha vista linked database (foi apagada). Adicionamos
    # o conteudo dinamico precedido pelos markers (com placeholder de aviso),
    # para que o setup_todo_view.py possa ser corrido depois e injectar a
    # vista persistente no topo SEM destruir o que esta dentro dos markers.
    fallback_intro: list[dict] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "⚠️"},
                "color": "yellow_background",
                "rich_text": rt(
                    "Vista 'Tarefas Hoje (David)' por configurar. "
                    "Correr setup_todo_view.py para activar interactividade."
                ),
            },
        },
        _marker_block(MARKER_START),
    ]
    payload = fallback_intro + list(new_content) + [_marker_block(MARKER_END)]
    await nc.append_blocks(page_id, payload)
    info["appended"] = len(payload)
    return info


# ----------------------------------------------------------------------
# Sprint 7 - Seccao 1: To-do (David) [REMOVIDA em Sprint 7.6]
# ----------------------------------------------------------------------
#
# A funcao _build_seccao_todo do Sprint 7 (callout link + counter) foi
# removida. A vista linked database persistente acima dos markers cobre
# esta funcionalidade de forma interactiva (David clica '+ Nova' para criar).
# O contador de tarefas continua a ser computado e devolvido no dict de
# output, para ser registado no Activity Log.

async def _count_tarefas_hoje(nc: NotionClient) -> tuple[int, dict[str, int]]:
    """Conta tarefas com 'Mostrar no Briefing' = true e 'Status' != Concluido,
    agrupando por Empresa. Devolve (total, dict empresa -> n).

    Tolerante a falhas: se a query falhar, devolve (0, {}).
    """
    try:
        filtro = {
            "and": [
                {"property": "Mostrar no Briefing", "checkbox": {"equals": True}},
                {"property": "Status", "status": {"does_not_equal": "Concluído"}},
            ]
        }
        resp = await nc.query_data_source(
            data_source_id=TAREFAS_DATA_SOURCE,
            filter_=filtro,
            page_size=100,
        )
    except Exception as exc:
        log.warning("count_tarefas_hoje FAIL", err=str(exc))
        return 0, {}

    results = resp.get("results", []) if isinstance(resp, dict) else []
    por_empresa: dict[str, int] = {}
    for page in results:
        props = page.get("properties", {}) or {}
        empresa = "Sem empresa"
        emp_prop = props.get("Empresa") or props.get("Cliente") or {}
        if emp_prop.get("type") == "select" and emp_prop.get("select"):
            empresa = emp_prop["select"].get("name") or empresa
        elif emp_prop.get("type") == "multi_select":
            opts = emp_prop.get("multi_select") or []
            if opts:
                empresa = opts[0].get("name") or empresa
        por_empresa[empresa] = por_empresa.get(empresa, 0) + 1

    return len(results), por_empresa


# ----------------------------------------------------------------------
# Sprint 7 - Seccao 2: Proximos 7 dias
# ----------------------------------------------------------------------

def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except Exception:
            try:
                return date.fromisoformat(value[:10])
            except Exception:
                return None
    return None


def _extract_prazo(item: dict) -> date | None:
    """Tenta encontrar uma data limite no metadata do item."""
    meta = item.get("metadata") or {}
    if not isinstance(meta, dict):
        return None
    for key in ("prazo", "prazo_propostas", "data_limite", "deadline", "data", "due"):
        v = meta.get(key)
        d = _parse_iso_date(v)
        if d:
            return d
    return None


def _acao_proposta(it: dict) -> str:
    """Accao proposta para um item na entrada do Activity Log."""
    tipo = it.get("tipo", "")
    prazo = _extract_prazo(it)
    if tipo == "concurso_novo":
        return f"Decidir ir/nao-ir ate {prazo.isoformat() if prazo else 's/ prazo'}"
    if tipo == "email_actionable":
        return "Responder (draft pronto no Gmail)"
    if tipo == "email_fatura_pendente":
        return "Extrair fatura manualmente"
    return "Rever e resolver"


def _build_seccao_proximos_7d(items: list[dict]) -> list[dict]:
    """Lista cronologica dos cards com prazo nos proximos 7 dias.

    NAO duplica cards: faz lista vertical breve com data + titulo + empresa,
    sem callouts de accao (o card completo ja aparece em URGENTE/SEMANA).
    """
    hoje = date.today()
    limite = hoje + timedelta(days=7)

    candidatos: list[tuple[date, dict]] = []
    for it in items:
        if it.get("tipo") not in DEADLINE_TIPOS:
            continue
        prazo = _extract_prazo(it)
        if prazo is None:
            continue
        if prazo < hoje or prazo > limite:
            continue
        candidatos.append((prazo, it))

    if not candidatos:
        return []

    candidatos.sort(key=lambda t: t[0])

    blocks: list[dict] = [heading(2, f"⏰ Próximos 7 dias  ({len(candidatos)})")]
    for prazo, it in candidatos:
        delta = (prazo - hoje).days
        if delta == 0:
            quando = "HOJE"
        elif delta == 1:
            quando = "amanhã"
        else:
            quando = f"em {delta}d ({prazo.isoformat()})"

        urg = it.get("urgencia", "P2")
        emoji = URGENCIA_MAP.get(urg, ("⚪", ""))[0]
        empresa = it.get("empresa") or ""
        emp_suffix = f"  ·  {empresa}" if empresa else ""

        rt_parts = [
            {"type": "text",
             "text": {"content": f"{quando}: "},
             "annotations": {"bold": True, "color": "red" if delta <= 1 else "orange"}},
            {"type": "text",
             "text": {"content": f"{emoji} {it.get('titulo','')}"}},
            {"type": "text",
             "text": {"content": emp_suffix},
             "annotations": {"color": EMPRESA_COLORS.get(empresa, "gray")}},
        ]
        blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rt_parts},
        })
    return blocks


# ----------------------------------------------------------------------
# Sprint 7 - Seccao 3: Drafts pendentes
# ----------------------------------------------------------------------

def _gmail_url(account: str, message_id: str) -> str:
    if not message_id:
        return f"https://mail.google.com/mail/u/?authuser={account}#inbox"
    return f"https://mail.google.com/mail/u/?authuser={account}#all/{message_id}"


def _empresa_for_inbox(inbox: str) -> str:
    inbox = (inbox or "").lower()
    if "omnai" in inbox:
        return "OMNAI"
    if "petinga" in inbox or "previnsa" in inbox:
        return "Previnsa"
    if "jmsoares" in inbox:
        return "JMSoares"
    if "sopato" in inbox:
        return "Sopato"
    return "Pessoal"


def _build_draft_card(d: dict) -> dict:
    subject = (d.get("subject") or "(sem assunto)")[:200]
    from_addr = (d.get("from_addr") or d.get("from") or "?")[:120]
    account = d.get("account") or d.get("inbox") or ""
    empresa = _empresa_for_inbox(account)
    preview = (d.get("preview") or d.get("draft_text") or d.get("full_text") or "")[:240]
    if len(preview) >= 240:
        preview = preview.rstrip() + "…"
    draft_url = d.get("url") or _gmail_url(account, d.get("gmail_message_id", ""))
    draft_id = d.get("draft_id") or ""

    try:
        token = make_token(draft_id, "draft-done")
        link_done = (
            f"{ACTIONS_BASE_URL.rstrip('/')}/actions/draft-done"
            f"?id={draft_id}&t={token}"
        )
    except Exception:
        link_done = ""

    title_parts = [
        {"type": "text",
         "text": {"content": f"Resposta para {from_addr}"},
         "annotations": {"bold": True}},
        {"type": "text",
         "text": {"content": f"  ·  {empresa}"},
         "annotations": {"color": EMPRESA_COLORS.get(empresa, "default")}},
        {"type": "text",
         "text": {"content": f"\n{subject}"},
         "annotations": {"italic": True, "color": "gray"}},
    ]

    actions_rt: list[dict] = [
        {"type": "text",
         "text": {"content": "\U0001f4e7 Abrir email", "link": {"url": _gmail_url(account, d.get('gmail_message_id', ''))}},
         "annotations": {"color": "blue"}},
        {"type": "text", "text": {"content": "    "}},
        {"type": "text",
         "text": {"content": "✏️ Editar draft", "link": {"url": draft_url}},
         "annotations": {"color": "purple"}},
    ]
    if link_done:
        actions_rt.extend([
            {"type": "text", "text": {"content": "    "}},
            {"type": "text",
             "text": {"content": "✓ Marcado respondido", "link": {"url": link_done}},
             "annotations": {"color": "green"}},
        ])

    children = [
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"type": "text",
                                      "text": {"content": preview},
                                      "annotations": {"color": "gray"}}]}},
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": actions_rt}},
    ]

    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "✉️"},
            "color": "purple_background",
            "rich_text": title_parts,
            "children": children,
        },
    }


async def _build_seccao_drafts() -> list[dict]:
    try:
        drafts = await peek_drafts()
    except Exception as exc:
        log.warning("peek_drafts FAIL", err=str(exc))
        return []

    drafts = (drafts or [])[:20]
    if not drafts:
        return []

    blocks: list[dict] = [heading(2, f"✉️ Drafts pendentes  ({len(drafts)})")]
    for d in drafts:
        blocks.append(_build_draft_card(d))
    return blocks


# ----------------------------------------------------------------------
# Sprint 7 - Seccao 4: Resumo arquivo emails 24h
# ----------------------------------------------------------------------

def _stat_int(stats: dict[str, str], *keys: str) -> int:
    for k in keys:
        v = stats.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


async def _build_seccao_resumo_emails() -> list[dict]:
    try:
        all_stats = await get_all_email_stats()
    except Exception as exc:
        log.warning("get_all_email_stats FAIL", err=str(exc))
        return []

    if not all_stats:
        return []

    blocks: list[dict] = [heading(2, "\U0001f4ca Resumo arquivo emails 24h")]

    header_cells = ["Inbox", "Lidos", "Classif.", "Drafts", "Faturas", "Arquivados"]
    rows: list[list[str]] = [header_cells]

    todas_inboxes = list(INBOX_ORDER)
    for k in all_stats.keys():
        if k not in todas_inboxes:
            todas_inboxes.append(k)

    total_lidos = total_class = total_drafts = total_inv = total_arch = 0
    for inbox in todas_inboxes:
        s = all_stats.get(inbox)
        if not s:
            continue
        lidos = _stat_int(s, "read")
        kept = _stat_int(s, "kept")
        classif = max(0, lidos - kept)
        drafts_n = _stat_int(s, "pending", "drafts")
        invoices_n = _stat_int(s, "invoices")
        arquivados = _stat_int(s, "archived")
        total_lidos += lidos
        total_class += classif
        total_drafts += drafts_n
        total_inv += invoices_n
        total_arch += arquivados
        short = inbox.split("@")[0] if "@" in inbox else inbox
        rows.append([short, str(lidos), str(classif), str(drafts_n), str(invoices_n), str(arquivados)])

    if len(rows) == 1:
        blocks.append(paragraph("Sem stats de email nas ultimas 24h."))
        return blocks

    rows.append(["TOTAL", str(total_lidos), str(total_class), str(total_drafts),
                 str(total_inv), str(total_arch)])

    table_rows: list[dict] = []
    for i, r in enumerate(rows):
        cells = []
        for j, val in enumerate(r):
            ann: dict[str, Any] = {}
            if i == 0 or i == len(rows) - 1:
                ann["bold"] = True
            cells.append([{"type": "text", "text": {"content": val}, "annotations": ann}])
        table_rows.append({
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": cells},
        })

    blocks.append({
        "object": "block",
        "type": "table",
        "table": {
            "table_width": len(header_cells),
            "has_column_header": True,
            "has_row_header": False,
            "children": table_rows,
        },
    })
    return blocks


# ----------------------------------------------------------------------
# Sprint 7 - Seccao 5: Faturas arquivadas 24h
# ----------------------------------------------------------------------

def _fatura_line(entry: dict) -> dict:
    filename = entry.get("filename") or entry.get("path") or entry.get("subject") or "(sem nome)"
    if isinstance(filename, str) and "/" in filename:
        filename = filename.rsplit("/", 1)[-1]
    empresa = entry.get("company") or entry.get("empresa") or ""
    total = entry.get("total")
    total_s = ""
    if total is not None:
        try:
            total_s = f"{float(total):.2f} €"
        except (TypeError, ValueError):
            total_s = str(total)

    rt_parts = [
        {"type": "text", "text": {"content": str(filename)[:160]}},
    ]
    if empresa:
        rt_parts.append({
            "type": "text",
            "text": {"content": f"  ·  {empresa}"},
            "annotations": {"color": EMPRESA_COLORS.get(empresa, "gray")},
        })
    if total_s:
        rt_parts.append({
            "type": "text",
            "text": {"content": f"  ·  {total_s}"},
            "annotations": {"bold": True, "color": "green"},
        })

    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rt_parts},
    }


async def _build_seccao_faturas() -> list[dict]:
    try:
        faturas = await peek_invoices()
    except Exception as exc:
        log.warning("peek_invoices FAIL", err=str(exc))
        return []

    faturas = (faturas or [])[:10]
    if not faturas:
        return []

    blocks: list[dict] = [heading(2, f"\U0001f4c4 Faturas arquivadas 24h  ({len(faturas)})")]
    for entry in faturas:
        if not isinstance(entry, dict):
            continue
        blocks.append(_fatura_line(entry))
    return blocks


# ----------------------------------------------------------------------
# Resumo Carlos (mantido inalterado)
# ----------------------------------------------------------------------

async def _resumo_executivo(items: list[dict], stats: dict[str, int]) -> str:
    if not items:
        return "Inbox limpo. Sem nada a tratar agora."

    counts = ", ".join(
        f"{n} {URGENCIA_MAP[u][1].lower()}"
        for u, n in sorted(stats.items())
        if u in URGENCIA_MAP and n > 0
    )

    sample = "\n".join(
        f"- {URGENCIA_MAP.get(i.get('urgencia','P2'),('','-'))[1]}: {i.get('titulo','')} ({i.get('empresa') or '-'})"
        for i in items[:8]
    )

    prompt = (
        f"Inbox executivo do David, {date.today().isoformat()}.\n"
        f"Contagens: {counts}.\n\n"
        f"Top items (max 8 mostrados):\n{sample}\n\n"
        "Escreve um paragrafo de resumo (max 3 frases) sobre o que e mais critico hoje."
    )
    try:
        return await generate(system=SYSTEM_CARLOS, prompt=prompt, max_tokens=400)
    except Exception as exc:
        log.warning("resumo_executivo FAIL", err=str(exc))
        return f"Inbox tem {sum(stats.values())} item(s) abertos. Tratar P0 primeiro."


# ----------------------------------------------------------------------
# Run principal
# ----------------------------------------------------------------------

async def run() -> dict:
    ano, semana, _ = date.today().isocalendar()
    hoje_iso = date.today().isoformat()

    sincronizados = 0
    tarefas_total = 0
    tarefas_por_empresa: dict[str, int] = {}

    try:
        async with NotionClient() as nc:
            sincronizados = await _sync_notion_to_db(nc)
            tarefas_total, tarefas_por_empresa = await _count_tarefas_hoje(nc)
    except Exception as exc:
        log.warning("notion sync FAIL", err=str(exc))

    # ----- Auto-expiracao e escalacao de concursos por prazo -----
    # Ordem importa: expirar primeiro (prazo passado -> dismissed), escalar
    # depois (prazo <= 5 dias e ainda aberto -> P0). Corre ANTES do list_open
    # para que o briefing de hoje ja reflicta o estado correcto.
    expirados = 0
    escalados = 0
    try:
        expirados = await briefing_db.expire_overdue_concursos()
        escalados = await briefing_db.escalate_concursos_by_prazo(dias=5)
    except Exception as exc:
        log.warning("expire/escalate concursos FAIL", err=str(exc))

    items = await briefing_db.list_open(limit=200)
    stats = await briefing_db.stats_por_urgencia()
    resolvidos = await briefing_db.list_recently_resolved(hours=24, limit=20)

    resumo = await _resumo_executivo(items, stats)

    # ----- Conteudo dinamico (entre markers) -----
    # NOTA Sprint 7.6: a seccao 'To-do (David)' Sprint 7 (callout link) NAO
    # entra mais no conteudo dinamico. A sua substituicao e a vista linked
    # database persistente acima do MARKER_START, gerida pelo setup script.
    blocks: list[dict] = [
        heading(1, f"Briefing {hoje_iso}"),
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "\U0001f305"},
                "rich_text": rt(
                    f"S{semana}/{ano} · "
                    f"{sum(stats.values())} item(s) abertos · "
                    f"P0: {stats.get('P0', 0)} · P1: {stats.get('P1', 0)} · "
                    f"P2: {stats.get('P2', 0)} · P3: {stats.get('P3', 0)}"
                ),
            },
        },
        paragraph(resumo),
    ]

    # ----- Sprint 7 seccao 2: Proximos 7 dias -----
    blocks.extend(_build_seccao_proximos_7d(items))

    # ----- Cards URGENTE / ESTA SEMANA / ACOMPANHAR / BAIXA -----
    if not items:
        blocks.append(heading(2, "✨ Inbox limpo"))
        blocks.append(paragraph(
            "Sem itens abertos. Quando os workers detectarem novidades, aparecerao aqui."
        ))
    else:
        agrupados: dict[str, list[dict]] = {u: [] for u in URGENCIA_MAP}
        for it in items:
            agrupados.setdefault(it.get("urgencia", "P2"), []).append(it)

        for urg in ("P0", "P1", "P2", "P3"):
            seccao = agrupados.get(urg, [])
            if not seccao:
                continue
            emoji, label = URGENCIA_MAP[urg]
            blocks.append(heading(2, f"{emoji} {label}  ({len(seccao)})"))
            for it in seccao:
                blocks.extend(_build_card(it))

    # ----- Sprint 7 seccao 3: Drafts pendentes -----
    drafts_blocks = await _build_seccao_drafts()
    if drafts_blocks:
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.extend(drafts_blocks)

    # ----- Sprint 7 seccao 4: Resumo arquivo emails 24h -----
    resumo_blocks = await _build_seccao_resumo_emails()
    if resumo_blocks:
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.extend(resumo_blocks)

    # ----- Sprint 7 seccao 5: Faturas arquivadas 24h -----
    faturas_blocks = await _build_seccao_faturas()
    if faturas_blocks:
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.extend(faturas_blocks)

    # ----- Resolvido nas ultimas 24h (mantido) -----
    if resolvidos:
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.append(heading(2, f"✅ Resolvido nas ultimas 24h  ({len(resolvidos)})"))
        for it in resolvidos:
            blocks.append(_build_resolved_card(it))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append(paragraph(
        f"Sincronizacao Notion→DB: {sincronizados} item(s) marcado(s) como resolvido."
    ))

    # ----- Regenerar area dinamica entre markers -----
    regen_info: dict[str, Any] = {}
    try:
        async with NotionClient() as nc:
            regen_info = await regenerate_dynamic_section(
                nc, MORNING_BRIEFING_PAGE, blocks
            )
    except Exception as exc:
        log.error("regenerate_dynamic_section FAIL", err=str(exc))
        raise

    # ----- Activity Log: entrada accionavel (top 10 P0/P1 + accao proposta) -----
    decisao = sorted(
        [i for i in items if i.get("urgencia") in ("P0", "P1")],
        key=lambda i: (i.get("urgencia", "P2"), _extract_prazo(i) or date.max),
    )[:10]

    al_children: list[dict] = [heading(2, "Itens que exigem decisao")]
    if decisao:
        for i in decisao:
            prazo_i = _extract_prazo(i)
            al_children.append(bullet(
                f"[{i.get('urgencia', 'P2')}] {(i.get('titulo') or '')[:120]} · "
                f"{i.get('empresa') or '-'} · "
                f"prazo {prazo_i.isoformat() if prazo_i else '-'} · "
                f"{_acao_proposta(i)}",
                link=i.get("link_origem"),
            ))
    else:
        al_children.append(paragraph("Sem itens P0/P1 abertos."))
    al_children.append(paragraph(resumo))

    # TODO: delta 'vs ontem' no titulo - guardar a contagem anterior em Redis
    # (services/state.py; chave sugerida omnai:briefing:last_open_count, no
    # padrao de set_email_stats). Omitido ate state.py expor um helper
    # generico get/set de contadores.
    abertos = sum(stats.values())
    try:
        await notion_ext.create_database_row(
            data_source_id=ACTIVITY_LOG_DB,
            title=(
                f"Briefing {hoje_iso} | {abertos} abertos | "
                f"P0:{stats.get('P0', 0)} P1:{stats.get('P1', 0)} | "
                f"{len(resolvidos)} resolvidos 24h | "
                f"{expirados} auto-expirados | {escalados} escalados P0"
            ),
            children=al_children,
        )
    except Exception:
        pass

    out = {
        "status": "ok",
        "data": hoje_iso,
        "items_abertos": sum(stats.values()),
        "stats": stats,
        "tarefas_briefing": tarefas_total,
        "tarefas_por_empresa": tarefas_por_empresa,
        "items_resolvidos_24h": len(resolvidos),
        "concursos_auto_expirados": expirados,
        "concursos_escalados_p0": escalados,
        "sincronizados_notion_db": sincronizados,
        "regen_mode": regen_info.get("mode"),
        "regen_deleted": regen_info.get("deleted", 0),
        "regen_appended": regen_info.get("appended", 0),
    }
    log.info("briefing-inbox", **out)
    return out
