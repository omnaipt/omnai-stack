"""Worker: briefing-inbox v10.0 (Sprint 10).

Mudancas vs v9.2.1:

Features adoptadas do briefing_carlos legado:
  1. Prioridades do dia (LLM extrai 3-5 P0/P1 dos briefing_items abertos,
     renderizadas como to_do com link).
  2. Pendente dos dias anteriores (carry-over): items abertos com
     criado_em < hoje (24h+) destacados em seccao propria.
  3. Tabela 8 inboxes detalhada (Lidos, Faturas, Arquiv., Apagad.,
     P/tratar, Manual, Rasc.).
  4. Faturas arquivadas hoje agrupadas por empresa (tabela detalhada).
  5. Faturas pendentes extraccao manual (lista + razao + link Gmail).
  6. Rascunhos com preview (200 chars) + checkbox \"Marcado respondido\"
     que aciona /actions/draft-done.

Bug fixes:
  A. Reload constante: usa regenerate_dynamic_section que preserva tudo
     acima do ultimo child_database (linked DB \"Tarefas Hoje\" no topo)
     em vez de replace_page_content total.
  B. Items resolvidos voltam a aparecer: stale-check ao iniciar -- para
     cada card de email com status=open, verifica via Gmail labels se o
     email ja saiu da INBOX. Se sim, marca done (motivo=archived_externally).
  C. Sincronizacao Notion->DB: to_dos checked com marker briefing-key
     fazem mark_done_by_chave (mantido).

Briefing accionavel (07-2026):
  D. Auto-expiracao de concursos vencidos + escalacao a P0 quando faltam
     <=5 dias de prazo (briefing_db.expire_overdue_concursos /
     escalate_concursos_by_prazo), a correr ANTES do list_open.
  E. Activity Log com corpo: top 10 itens P0/P1 (empresa, prazo, accao
     proposta) + resumo executivo, em vez de pagina vazia. Titulo ganha
     contagens P0/P1, auto-expirados e escalados.

Restricoes preservadas:
  * filtro responsabilidade David (briefing_db ja faz isto upstream)
  * seccao Resolvido nas ultimas 24h
  * archive Gmail no Resolver/Dispensar (continua via /actions/done
    que chama briefing_db.mark_done + gmail_archive)
  * NAO chama briefing_carlos.run() (legado deprecated)
"""
from __future__ import annotations

import asyncio
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
from services.state import (
    get_all_email_stats,
    peek_drafts,
    peek_invoices,
    peek_manual_invoices,
    peek_pending_emails,
)

log = structlog.get_logger()


MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
ACTIONS_BASE_URL = os.getenv("ACTIONS_BASE_URL", "https://agents.omnai.pt")
MAIL_TOKEN_PREFIX = f"{ACTIONS_BASE_URL.rstrip('/')}/mail/"

# Limite de cards a verificar via Gmail no stale-check (Bug B/C)
STALE_CHECK_LIMIT = int(os.getenv("BRIEFING_STALE_CHECK_LIMIT", "50"))
STALE_CHECK_HOURS = int(os.getenv("BRIEFING_STALE_CHECK_HOURS", "48"))

# Escalacao de concursos: P0 quando faltam <= N dias de prazo
CONCURSO_ESCALATE_DIAS = int(os.getenv("BRIEFING_CONCURSO_ESCALATE_DIAS", "5"))

URGENCIA_MAP = {
    "P0": ("🔴", "URGENTE"),
    "P1": ("🟡", "ESTA SEMANA"),
    "P2": ("🟢", "ACOMPANHAR"),
    "P3": ("⚪", "BAIXA PRIORIDADE"),
}

EMPRESA_COLORS = {
    "OMNAI": "blue",
    "Previnsa": "green",
    "JMSoares": "orange",
    "Sopato": "purple",
    "Pessoal": "gray",
}

EMAIL_ACCOUNTS = [
    {"account": "david.sardinha@previnsa.com", "label": "Previnsa (Gmail forward -> Carlos)"},
    {"account": "david.sardinha@jmsoares.pt",  "label": "JMSoares (david@jmsoares.pt)"},
    {"account": "david.sardinha@omnai.pt",     "label": "OMNAI David (david@omnai.pt - IMAP Hostinger)"},
    {"account": "hello@omnai.pt",              "label": "OMNAI geral (hello@omnai.pt - IMAP Hostinger)"},
    {"account": "david.sardinha@sapo.pt",      "label": "Pessoal Sapo (david.sardinha@sapo.pt - IMAP)"},
    {"account": "davidsardinhalves@gmail.com", "label": "Plataformas IA (davidsardinhalves@gmail.com)"},
    {"account": "sopato.cascais@gmail.com",    "label": "Sopato imobiliaria (sopato.cascais@gmail.com)"},
    {"account": "opaidapetinga@gmail.com",     "label": "Carlos dispatcher (opaidapetinga@gmail.com)"},
]

GMAIL_DOMAINS = {
    "davidsardinhalves@gmail.com",
    "sopato.cascais@gmail.com",
    "opaidapetinga@gmail.com",
}

SYSTEM_CARLOS = (
    "Es o Carlos, Chief of Staff do David. Resume em UM unico paragrafo curto "
    "(maximo 3 frases) o estado do inbox executivo. Portugues europeu, tu directo, "
    "sem cliches, sem travessoes. NAO inventes tarefas. NAO faces listas. "
    "Foca-te no que e mais critico hoje e no que pode esperar."
)

SYSTEM_PRIORIDADES = (
    "Es o Carlos, Chief of Staff. A partir da lista de items abertos do inbox "
    "do David, escolhe os 3 a 5 MAIS criticos para hoje. Devolves SO uma lista "
    "JSON: [{\"id\": \"<id>\", \"texto\": \"<accao directa>\"}]. "
    "Nao expliques. Sem markdown. Texto em portugues europeu, directo, com verbo. "
    "Maximo 80 chars por item. Prioriza P0 e P1, com empresa entre parenteses se relevante."
)


CHAVE_MARKER = re.compile(r"<!--briefing-key:([0-9a-f]{64})-->")
DYNAMIC_START_MARKER = "<!--briefing:dynamic:start-->"
DYNAMIC_END_MARKER = "<!--briefing:dynamic:end-->"


# ----------------------------------------------------------------------
# Helpers de blocos Notion (locais, evitam dependencia de utils.notion_blocks)
# ----------------------------------------------------------------------

def _rt_array(text: str, link: str | None = None, **annotations: Any) -> list[dict]:
    text_obj: dict[str, Any] = {"content": (text or "")[:2000]}
    if link:
        text_obj["link"] = {"url": link}
    block: dict[str, Any] = {"type": "text", "text": text_obj}
    if annotations:
        block["annotations"] = annotations
    return [block]


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _callout(text: str, emoji: str = "📌", color: str | None = None) -> dict:
    body: dict[str, Any] = {
        "icon": {"type": "emoji", "emoji": emoji},
        "rich_text": rt(text),
    }
    if color:
        body["color"] = color
    return {"object": "block", "type": "callout", "callout": body}


def _to_do(text: str, link: str | None = None, checked: bool = False) -> dict:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": _rt_array(text, link=link),
            "checked": checked,
        },
    }


def _paragraph_link(text: str, url: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rt_array(text, link=url)},
    }


def _table(rows: list[list[list[dict]]], has_column_header: bool = True) -> dict:
    if not rows:
        return _paragraph("(tabela vazia)")
    cols = max(len(r) for r in rows)
    children = []
    for row in rows:
        cells = list(row) + [_rt_array("")] * (cols - len(row))
        children.append({
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": cells},
        })
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": cols,
            "has_column_header": has_column_header,
            "has_row_header": False,
            "children": children,
        },
    }


def _paragraph(text: str) -> dict:
    return paragraph(text)


# ----------------------------------------------------------------------
# Sync Notion -> DB (mantido da v9.2.1)
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
# Bug fix B/C: stale-check de cards de email
# ----------------------------------------------------------------------

async def _stale_check_emails(items: list[dict]) -> int:
    """Para cards tipo email_*, verifica se email ainda esta em INBOX no Gmail.

    Se nao estiver, marca briefing_item como done com motivo
    'archived_externally'. Limite STALE_CHECK_LIMIT por run, focado nos
    items criados nas ultimas STALE_CHECK_HOURS horas.

    Retorna numero de items marcados.
    """
    try:
        from services import gmail as gmail_svc
    except Exception as exc:
        log.warning("stale_check.gmail_import_fail", err=str(exc))
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_CHECK_HOURS)
    marcados = 0
    checks = 0

    for it in items:
        if checks >= STALE_CHECK_LIMIT:
            break
        tipo = (it.get("tipo") or "").lower()
        if not tipo.startswith("email"):
            continue

        criado = it.get("criado_em")
        if isinstance(criado, datetime):
            if criado.tzinfo is None:
                criado = criado.replace(tzinfo=timezone.utc)
            if criado < cutoff:
                continue

        meta = it.get("metadata") or {}
        if isinstance(meta, str):
            try:
                import json as _json
                meta = _json.loads(meta)
            except Exception:
                meta = {}

        account = meta.get("account") or meta.get("inbox") or ""
        msg_id = meta.get("gmail_message_id") or meta.get("message_id") or ""
        if not account or not msg_id or account not in GMAIL_DOMAINS:
            continue

        checks += 1
        try:
            summary = await asyncio.to_thread(gmail_svc.summarize_message, account, msg_id)
        except Exception as exc:
            log.warning("stale_check.summarize_fail", account=account, err=str(exc))
            continue

        if not summary:
            continue
        labels = summary.get("labels") or []
        if "INBOX" in labels:
            continue

        try:
            chave = it.get("chave")
            if chave and await mark_done_by_chave(chave):
                marcados += 1
                log.info(
                    "stale_check.archived_externally",
                    chave=chave[:12], account=account,
                )
        except Exception as exc:
            log.warning("stale_check.mark_fail", err=str(exc))

    log.info("stale_check.summary", checks=checks, marcados=marcados)
    return marcados


# ----------------------------------------------------------------------
# Cards (mantidos da v9.2.1)
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
        {"type": "text", "text": {"content": "🗑 Dispensar", "link": {"url": link_dismiss}}, "annotations": {"color": "gray"}},
    ]
    if item.get("link_origem"):
        actions_rt = [
            {"type": "text", "text": {"content": "🔗 Abrir", "link": {"url": item["link_origem"]}}, "annotations": {"color": "blue"}},
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

    icon = "✅" if status == "done" else "🗑️"
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
# Resumo executivo + Prioridades do dia
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


async def _prioridades_do_dia(items: list[dict]) -> list[dict]:
    """LLM extrai 3-5 items mais criticos. Devolve [{id, texto, link_origem}]."""
    if not items:
        return []

    candidatos = [i for i in items if i.get("urgencia") in ("P0", "P1")]
    if len(candidatos) < 3:
        candidatos = candidatos + [i for i in items if i.get("urgencia") == "P2"]
    candidatos = candidatos[:15]

    if not candidatos:
        return []

    sample_lines = []
    for i in candidatos:
        sample_lines.append(
            f"- id={i['id']} | {i.get('urgencia','P2')} | "
            f"{(i.get('titulo') or '')[:120]} | {i.get('empresa') or '-'}"
        )

    prompt = (
        f"Items abertos no inbox executivo, {date.today().isoformat()}:\n\n"
        + "\n".join(sample_lines)
        + "\n\nDevolve JSON com 3 a 5 prioridades, formato: "
        '[{"id": "<id>", "texto": "<accao curta com verbo>"}]'
    )

    try:
        raw = await generate(system=SYSTEM_PRIORIDADES, prompt=prompt, max_tokens=600)
    except Exception as exc:
        log.warning("prioridades.llm_fail", err=str(exc))
        return _prioridades_fallback(candidatos)

    import json as _json
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        parsed = _json.loads(raw)
    except Exception:
        log.warning("prioridades.parse_fail", raw_head=raw[:120])
        return _prioridades_fallback(candidatos)

    if not isinstance(parsed, list):
        return _prioridades_fallback(candidatos)

    by_id = {str(i["id"]): i for i in candidatos}
    out: list[dict] = []
    for entry in parsed[:5]:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("id") or "")
        texto = (entry.get("texto") or "").strip()
        if not eid or not texto:
            continue
        item = by_id.get(eid)
        if not item:
            continue
        out.append({
            "id": eid,
            "texto": texto[:200],
            "link_origem": item.get("link_origem"),
            "chave": item.get("chave"),
        })
    if not out:
        return _prioridades_fallback(candidatos)
    return out


async def _guardar_briefing_do_dia(resumo: str, prioridades: list[dict],
                                   stats: dict) -> None:
    """Persiste o briefing para a Home o poder mostrar sem chamar o LLM.

    Antes isto só existia dentro da página Notion. A Home lê daqui.
    """
    import json as _json

    from services import briefing_db as _db
    try:
        pool = await _db._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS briefing_dia (
                    data        date PRIMARY KEY,
                    resumo      text,
                    prioridades jsonb NOT NULL DEFAULT '[]'::jsonb,
                    stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
                    gerado_em   timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO briefing_dia (data, resumo, prioridades, stats, gerado_em)
                VALUES (current_date, $1, $2::jsonb, $3::jsonb, now())
                ON CONFLICT (data) DO UPDATE SET
                    resumo = EXCLUDED.resumo,
                    prioridades = EXCLUDED.prioridades,
                    stats = EXCLUDED.stats,
                    gerado_em = now()
                """,
                resumo, _json.dumps(prioridades or []), _json.dumps(stats or {}),
            )
    except Exception as exc:
        log.warning("briefing_dia.guardar_falhou", err=str(exc)[:200])


def _prioridades_fallback(items: list[dict]) -> list[dict]:
    """Fallback determinista: top P0/P1 por ordem original, max 5."""
    out: list[dict] = []
    for i in items[:5]:
        out.append({
            "id": str(i["id"]),
            "texto": (i.get("titulo") or "")[:160],
            "link_origem": i.get("link_origem"),
            "chave": i.get("chave"),
        })
    return out


# ----------------------------------------------------------------------
# Tabela 8 inboxes
# ----------------------------------------------------------------------

def _build_email_table(email_stats: dict, drafts_by_account: dict) -> dict:
    header = [
        _rt_array("Conta"), _rt_array("Lidos"), _rt_array("Faturas"),
        _rt_array("Arquiv."), _rt_array("Apagad."), _rt_array("P/tratar"),
        _rt_array("Manual"), _rt_array("Rasc."),
    ]
    rows = [header]
    for acc in EMAIL_ACCOUNTS:
        s = email_stats.get(acc["account"], {})
        drafts_n = drafts_by_account.get(acc["account"], 0)
        rows.append([
            _rt_array(acc["label"]),
            _rt_array(str(s.get("read", "—"))),
            _rt_array(str(s.get("invoices", "—"))),
            _rt_array(str(s.get("archived", "—"))),
            _rt_array(str(s.get("deleted", "—"))),
            _rt_array(str(s.get("pending", "—"))),
            _rt_array(str(s.get("manual_invoices", "—"))),
            _rt_array(str(drafts_n) if drafts_n else "—"),
        ])
    return _table(rows, has_column_header=True)


# ----------------------------------------------------------------------
# Faturas arquivadas hoje (por empresa)
# ----------------------------------------------------------------------

def _build_invoices_archived_today(invoices: list[dict]) -> list[dict]:
    blocks: list[dict] = [heading(2, f"📁 Faturas arquivadas hoje  ({len(invoices)})")]
    if not invoices:
        blocks.append(_callout("Sem faturas arquivadas hoje.", emoji="📂"))
        return blocks

    by_company: dict[str, list[dict]] = {}
    total_amount: float = 0.0
    for inv in invoices:
        c = inv.get("company") or "Outros"
        by_company.setdefault(c, []).append(inv)
        try:
            total_amount += float(inv.get("amount") or 0)
        except Exception:
            pass

    blocks.append(_paragraph(
        f"{len(invoices)} fatura(s) arquivada(s) hoje. "
        f"Valor total estimado: {total_amount:.2f} EUR (aproximado)."
    ))

    for company in sorted(by_company.keys()):
        items = by_company[company]
        blocks.append(heading(3, f"{company} ({len(items)})"))
        header = [
            _rt_array("Fornecedor"), _rt_array("Data"),
            _rt_array("Valor"), _rt_array("Trimestre"), _rt_array("Ficheiro"),
        ]
        rows = [header]
        for inv in items:
            amount = inv.get("amount")
            cur = inv.get("currency", "EUR")
            amt_str = f"{float(amount):.2f} {cur}" if amount else "—"
            rows.append([
                _rt_array(str(inv.get("supplier") or "—")),
                _rt_array(str(inv.get("date") or "—")),
                _rt_array(amt_str),
                _rt_array(str(inv.get("quarter") or "—")),
                _rt_array(str(inv.get("filename", ""))[-50:]),
            ])
        blocks.append(_table(rows, has_column_header=True))
    return blocks


# ----------------------------------------------------------------------
# Faturas pendentes extraccao manual
# ----------------------------------------------------------------------

def _build_manual_invoices(manual: list[dict]) -> list[dict]:
    blocks: list[dict] = [heading(2, f"🧾 Faturas pendentes extracção manual  ({len(manual)})")]
    if not manual:
        blocks.append(_callout("Sem faturas para extracção manual.", emoji="✅"))
        return blocks
    blocks.append(_paragraph(
        f"{len(manual)} email(s) classificados como fatura mas o Carlos não conseguiu "
        "extrair o PDF automaticamente. Abre cada um e arquiva manualmente."
    ))
    for m in manual[:30]:
        subj = (m.get("subject") or "(sem assunto)")[:80]
        frm = (m.get("from") or "")[:40]
        inbox = m.get("inbox") or m.get("account") or ""
        label = f"→ {subj} | {frm} | {inbox}"
        url = m.get("url", "")
        if url:
            blocks.append(_paragraph_link(label, url))
        else:
            blocks.append(_paragraph(label))
        if m.get("reason"):
            blocks.append(_callout(f"Razão: {m['reason']}", emoji="⚠️"))
    return blocks


# ----------------------------------------------------------------------
# Drafts com preview + checkbox draft-done
# ----------------------------------------------------------------------

def _draft_id(d: dict) -> str | None:
    return (
        d.get("id")
        or d.get("draft_id")
        or d.get("gmail_draft_id")
        or d.get("thread_id")
        or d.get("threadId")
    )


def _build_drafts_section(drafts: list[dict]) -> list[dict]:
    blocks: list[dict] = [heading(2, f"✉️ Rascunhos de resposta ({len(drafts)})")]
    if not drafts:
        blocks.append(_callout("Sem rascunhos pendentes.", emoji="✉️"))
        return blocks

    for d in drafts[:25]:
        subj = (d.get("subject") or "(sem assunto)")[:120]
        from_addr = (d.get("from_addr") or d.get("from") or "?")[:60]
        account = d.get("account") or "?"
        url = d.get("url") or ""
        label = f"→ {subj} | De: {from_addr} | Caixa: {account}"
        if url:
            blocks.append(_paragraph_link(label, url))
        else:
            blocks.append(_paragraph(label))

        preview = d.get("preview") or d.get("draft_text") or d.get("body") or ""
        if preview:
            blocks.append(_callout(preview[:200], emoji="📝"))

        # Checkbox 'Marcado respondido' -> /actions/draft-done
        did = _draft_id(d)
        if did:
            try:
                token = make_token(str(did), "draft-done")
                draft_url = (
                    f"{ACTIONS_BASE_URL.rstrip('/')}/actions/draft-done"
                    f"?id={did}&t={token}"
                )
                blocks.append(_to_do(
                    "Marcado respondido",
                    link=draft_url,
                    checked=False,
                ))
            except Exception as exc:
                log.warning("draft.token_fail", id=str(did), err=str(exc))

    return blocks


# ----------------------------------------------------------------------
# Pending emails (checkbox -> auto-archive na proxima corrida)
# ----------------------------------------------------------------------

def _build_pending_emails(pending: list[dict]) -> list[dict]:
    if not pending:
        return []
    blocks: list[dict] = [
        heading(2, f"📧 Emails pendentes de acção (marca ✅ para arquivar)  ({len(pending)})"),
        _paragraph(
            "Marca o checkbox nos que queres arquivar. Na próxima corrida "
            "(midday/evening/morning) o Carlos arquiva-os automaticamente nas "
            "pastas correctas das respectivas contas."
        ),
    ]
    for p in pending[:40]:
        subj = (p.get("subject") or "(sem assunto)")[:120]
        frm = (p.get("from") or "?")[:60]
        title = f"{subj} — de {frm}"
        url = MAIL_TOKEN_PREFIX + (p.get("token") or "")
        blocks.append(_to_do(title[:2000], link=url, checked=False))
    return blocks


# ----------------------------------------------------------------------
# Carry-over: items abertos de dias anteriores (criado_em > 24h)
# ----------------------------------------------------------------------

def _split_carryover(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(novos_hoje, carry_over). Carry-over = criado_em < hoje 00:00 UTC."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    novos: list[dict] = []
    carry: list[dict] = []
    for it in items:
        criado = it.get("criado_em")
        if isinstance(criado, datetime):
            c = criado if criado.tzinfo else criado.replace(tzinfo=timezone.utc)
            if c < today_start:
                carry.append(it)
            else:
                novos.append(it)
        else:
            novos.append(it)
    return novos, carry


# ----------------------------------------------------------------------
# Briefing accionavel (07-2026): helpers do Activity Log
# ----------------------------------------------------------------------

def _extract_prazo(item: dict) -> date | None:
    """Extrai o prazo (date) do metadata do item, se existir.

    Reconhece a chave 'prazo_propostas' (formato ISO, usada pelos cards
    de concursos) com fallback para 'prazo'. Valores nao-ISO (ex.:
    'sem prazo') devolvem None. metadata pode chegar como str (asyncpg
    devolve jsonb como texto sem codec registado).
    """
    meta = item.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except Exception:
            return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("prazo_propostas") or meta.get("prazo") or ""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except Exception:
        return None


def _acao_proposta(item: dict) -> str:
    """Accao proposta curta para a pagina do Activity Log."""
    tipo = item.get("tipo", "")
    prazo = _extract_prazo(item)
    if tipo == "concurso_novo":
        return f"Decidir ir/nao-ir ate {prazo.isoformat() if prazo else 's/ prazo'}"
    if tipo == "email_actionable":
        return "Responder (draft pronto no Gmail)"
    if tipo == "email_fatura_pendente":
        return "Extrair fatura manualmente"
    return "Rever e resolver"


def _build_activity_log_children(items: list[dict], resumo: str) -> list[dict]:
    """Corpo da entrada do Activity Log: top 10 itens P0/P1 que exigem
    decisao (ordenados por urgencia e prazo) + resumo executivo.
    """
    decisao = sorted(
        [i for i in items if i.get("urgencia") in ("P0", "P1")],
        key=lambda i: (i.get("urgencia", "P2"), _extract_prazo(i) or date.max),
    )[:10]

    children: list[dict] = [heading(2, "Itens que exigem decisao")]
    if decisao:
        for i in decisao:
            prazo = _extract_prazo(i)
            children.append(bullet(
                f"[{i.get('urgencia', 'P?')}] {(i.get('titulo') or '')[:120]} · "
                f"{i.get('empresa') or '-'} · "
                f"prazo {prazo.isoformat() if prazo else '-'} · "
                f"{_acao_proposta(i)}",
                link=i.get("link_origem"),
            ))
    else:
        children.append(paragraph("Sem itens P0/P1 abertos."))
    children.append(paragraph(resumo))
    return children


# ----------------------------------------------------------------------
# Bug fix A: regenerate_dynamic_section
# ----------------------------------------------------------------------

async def _regenerate_dynamic_section(nc: NotionClient, blocks: list[dict]) -> None:
    """Apaga blocks dinamicos preservando todo o conteudo ate ao ultimo
    child_database (linked DB 'Tarefas Hoje (David)') no topo da pagina.

    Estrategia:
      1. GET children da pagina.
      2. Encontrar indice do ULTIMO child_database -- esse e o anchor.
      3. Apagar todos os blocks DEPOIS desse anchor.
      4. Append os novos blocks.

    Se nao houver child_database (primeira corrida ou pagina vazia),
    fallback seguro: replace_page_content total.
    """
    try:
        existing = await nc.get_block_children(MORNING_BRIEFING_PAGE)
    except Exception as exc:
        log.warning("regen.read_fail", err=str(exc))
        # Fallback: usa replace total
        await nc.replace_page_content(MORNING_BRIEFING_PAGE, blocks)
        return

    anchor_idx = -1
    for idx, b in enumerate(existing):
        if b.get("type") == "child_database":
            anchor_idx = idx

    if anchor_idx < 0:
        log.warning("regen.no_anchor_fallback_full_replace")
        await nc.replace_page_content(MORNING_BRIEFING_PAGE, blocks)
        return

    # Apagar tudo depois do anchor
    to_delete = existing[anchor_idx + 1:]
    for b in to_delete:
        try:
            await nc.delete_block(b["id"])
        except Exception as exc:
            log.warning("regen.delete_fail", block_id=b.get("id"), err=str(exc))

    # Append novos blocks
    if blocks:
        await nc.append_blocks(MORNING_BRIEFING_PAGE, blocks)

    log.info("regen.done", anchor_idx=anchor_idx, deleted=len(to_delete), appended=len(blocks))


# ----------------------------------------------------------------------
# Run principal
# ----------------------------------------------------------------------

async def run() -> dict:
    ano, semana, _ = date.today().isocalendar()
    hoje_iso = date.today().isoformat()

    # Step 1: sincronizar Notion->DB (checkboxes marcados manualmente)
    sincronizados = 0
    try:
        async with NotionClient() as nc:
            sincronizados = await _sync_notion_to_db(nc)
    except Exception as exc:
        log.warning("notion sync FAIL", err=str(exc))

    # Step 1b: lifecycle de concursos ANTES de ler os items abertos.
    # Ordem importa: expirar primeiro (mata vencidos), escalar depois
    # (sobe a P0 os que estao a <=N dias). Em try/except individual --
    # o briefing nao pode morrer por causa disto.
    expirados = 0
    escalados = 0
    try:
        expirados = await briefing_db.expire_overdue_concursos()
    except Exception as exc:
        log.warning("expire_overdue_concursos FAIL", err=str(exc))
    try:
        escalados = await briefing_db.escalate_concursos_by_prazo(
            dias=CONCURSO_ESCALATE_DIAS
        )
    except Exception as exc:
        log.warning("escalate_concursos_by_prazo FAIL", err=str(exc))
    if expirados or escalados:
        log.info(
            "concursos.lifecycle",
            expirados=expirados, escalados=escalados,
            dias=CONCURSO_ESCALATE_DIAS,
        )

    # Step 2: ler items abertos
    items = await briefing_db.list_open(limit=200)

    # Step 3: stale-check (Bug B/C). Pode marcar items como done.
    archived_externally = 0
    try:
        archived_externally = await _stale_check_emails(items)
    except Exception as exc:
        log.warning("stale_check FAIL", err=str(exc))

    # Re-ler items se stale-check marcou alguns
    if archived_externally:
        items = await briefing_db.list_open(limit=200)

    stats = await briefing_db.stats_por_urgencia()
    resolvidos = await briefing_db.list_recently_resolved(hours=24, limit=20)

    # Step 4: split carry-over vs novos
    novos, carry = _split_carryover(items)

    # Step 5: dados Redis (state)
    try:
        email_stats = await get_all_email_stats()
    except Exception:
        email_stats = {}
    try:
        drafts_raw = await peek_drafts()
    except Exception:
        drafts_raw = []
    try:
        invoices = await peek_invoices()
    except Exception:
        invoices = []
    try:
        manual_invoices = await peek_manual_invoices()
    except Exception:
        manual_invoices = []
    try:
        pending_emails = await peek_pending_emails()
    except Exception:
        pending_emails = []

    drafts_by_account: dict[str, int] = {}
    for d in drafts_raw:
        acc = d.get("account", "")
        drafts_by_account[acc] = drafts_by_account.get(acc, 0) + 1

    # Step 6: LLM resume + prioridades
    resumo = await _resumo_executivo(items, stats)
    prioridades = await _prioridades_do_dia(items)
    await _guardar_briefing_do_dia(resumo, prioridades, stats)

    # Step 7: construir blocks
    blocks: list[dict] = [
        # Marker invisivel para identificar inicio dos dynamic blocks
        _paragraph(DYNAMIC_START_MARKER),
        heading(1, f"Briefing {hoje_iso}"),
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "🌅"},
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

    # Prioridades do dia (LLM)
    if prioridades:
        blocks.append(heading(2, f"🔥 Prioridades do dia ({len(prioridades)})"))
        for p in prioridades:
            blocks.append(_to_do(
                p["texto"],
                link=p.get("link_origem"),
                checked=False,
            ))

    # Carry-over
    if carry:
        blocks.append(heading(2, f"📌 Pendente dos dias anteriores  ({len(carry)})"))
        agrupados_c: dict[str, list[dict]] = {u: [] for u in URGENCIA_MAP}
        for it in carry:
            agrupados_c.setdefault(it.get("urgencia", "P2"), []).append(it)
        for urg in ("P0", "P1", "P2", "P3"):
            seccao = agrupados_c.get(urg, [])
            for it in seccao:
                blocks.extend(_build_card(it))

    # Cards novos por urgencia (so items criados hoje)
    if novos:
        agrupados: dict[str, list[dict]] = {u: [] for u in URGENCIA_MAP}
        for it in novos:
            agrupados.setdefault(it.get("urgencia", "P2"), []).append(it)
        for urg in ("P0", "P1", "P2", "P3"):
            seccao = agrupados.get(urg, [])
            if not seccao:
                continue
            emoji, label = URGENCIA_MAP[urg]
            blocks.append(heading(2, f"{emoji} {label}  ({len(seccao)})"))
            for it in seccao:
                blocks.extend(_build_card(it))
    elif not carry and not items:
        blocks.append(heading(2, "✨ Inbox limpo"))
        blocks.append(_paragraph(
            "Sem itens abertos. Quando os workers detectarem novidades, aparecerao aqui."
        ))

    # Pending emails (checkbox -> auto-archive)
    blocks.extend(_build_pending_emails(pending_emails))

    # Drafts com preview + checkbox marcado respondido
    blocks.append(_divider())
    blocks.extend(_build_drafts_section(drafts_raw))

    # Tabela 8 inboxes
    blocks.append(_divider())
    blocks.append(heading(2, "📊 Estado das caixas de correio"))
    blocks.append(_paragraph(
        "Previnsa é tratada via opaidapetinga@gmail.com (forwarding)."
    ))
    blocks.append(_build_email_table(email_stats, drafts_by_account))

    # Faturas arquivadas hoje
    blocks.append(_divider())
    blocks.extend(_build_invoices_archived_today(invoices))

    # Faturas pendentes manual
    blocks.append(_divider())
    blocks.extend(_build_manual_invoices(manual_invoices))

    # Resolvido nas ultimas 24h
    if resolvidos:
        blocks.append(_divider())
        blocks.append(heading(2, f"✅ Resolvido nas ultimas 24h  ({len(resolvidos)})"))
        for it in resolvidos:
            blocks.append(_build_resolved_card(it))

    # Footer sync
    blocks.append(_divider())
    extra_archive = (
        f" · {archived_externally} arquivado(s) externamente"
        if archived_externally else ""
    )
    extra_concursos = ""
    if expirados or escalados:
        extra_concursos = (
            f" · {expirados} concurso(s) auto-expirado(s)"
            f" · {escalados} escalado(s) a P0"
        )
    blocks.append(_paragraph(
        f"Sincronização Notion→DB: {sincronizados} item(s) marcado(s) como resolvido"
        f"{extra_archive}{extra_concursos}."
    ))
    blocks.append(_paragraph(DYNAMIC_END_MARKER))

    # Step 8: render -- usa regenerate_dynamic_section (Bug A fix)
    try:
        async with NotionClient() as nc:
            await _regenerate_dynamic_section(nc, blocks)
    except Exception as exc:
        log.error("regen FAIL", err=str(exc))
        raise

    # Step 9: log no Activity Log (titulo com contagens + corpo accionavel)
    # TODO(delta vs ontem): persistir a contagem de abertos em Redis
    # (services/state.py, padrao set_email_stats; chave sugerida
    # omnai:briefing:last_open_count) e mostrar "(+N vs ontem)" no titulo.
    try:
        await notion_ext.create_database_row(
            data_source_id=ACTIVITY_LOG_DB,
            title=(
                f"Briefing inbox {hoje_iso} | {sum(stats.values())} abertos | "
                f"P0:{stats.get('P0', 0)} P1:{stats.get('P1', 0)} | "
                f"{len(resolvidos)} resolvidos 24h | "
                f"{expirados} auto-expirados | {escalados} escalados P0 | "
                f"sync={sincronizados} | stale={archived_externally}"
            ),
            children=_build_activity_log_children(items, resumo),
        )
    except Exception as exc:
        log.warning("activity_log FAIL", err=str(exc))

    out = {
        "status": "ok",
        "data": hoje_iso,
        "items_abertos": sum(stats.values()),
        "items_carry_over": len(carry),
        "items_novos_hoje": len(novos),
        "stats": stats,
        "items_resolvidos_24h": len(resolvidos),
        "sincronizados_notion_db": sincronizados,
        "archived_externally": archived_externally,
        "concursos_auto_expirados": expirados,
        "concursos_escalados_p0": escalados,
        "prioridades_count": len(prioridades),
        "drafts_pending": len(drafts_raw),
        "invoices_today": len(invoices),
        "manual_invoices": len(manual_invoices),
        "pending_emails": len(pending_emails),
    }
    log.info("briefing-inbox v10", **out)
    return out
