"""briefing-carlos v0.5.0 - dedup endurecido + carryover-aware LLM.

Mudancas vs v0.4.0:
1. _extract_carryover agora devolve lista de tuplos (text, normalized_key) e
   deduplica mais agressivamente: junta stop-words, aceita variantes de wording,
   usa chave de 25 chars alfanumericos lower.
2. Novas funcoes _dedup_by_key() aplicadas a pending_emails (por token) e a
   drafts (por URL ou thread-id + account) antes de renderizar.
3. Carryover passa a ser injectado no prompt LLM como "nao repitas estes items",
   para evitar duplicacao entre Prioridades geradas pelo LLM e Pendente dos dias
   anteriores.
4. Log adicional com estatisticas de dedup para auditoria.

Nao mexe em Redis nem em snapshot externo: o proprio briefing guardado no
Notion ja e a fonte de verdade (itens unchecked = por tratar, checked ou
apagados = tratados).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog

from services.llm import generate
from services.notion import NotionClient
from services.state import (
    get_all_email_stats, peek_drafts, peek_invoices, peek_manual_invoices,
    peek_pending_emails,
)
from utils.notion_blocks import (
    bookmark, bullet, callout, divider, heading_1, heading_2,
    markdown_to_blocks, paragraph, paragraph_link, rt_array, table, to_do,
)

log = structlog.get_logger()

MORNING_BRIEFING_PAGE_ID = "33e973b9-2387-8107-8667-eadc9128ab27"
MAIL_TOKEN_PREFIX = "https://agents.omnai.pt/mail/"

ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
TAREFAS_DB = "0135c7f8-2823-4287-9556-7968fd36998d"
PIPELINE_DB = "6db8052e-4e52-4099-bff6-0b21c50b8396"
CONTRATOS_DB = "f5dca372-90dc-4157-9725-70d0a03072d9"
DECISOES_DB = "7633f270-9e14-4904-8401-360608262c02"

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

# v0.5.0: stop-words aumentada para cortar ruido em dedup de carryover.
_STOP_WORDS: set[str] = {
    "o", "a", "os", "as", "de", "da", "do", "das", "dos", "e", "ou", "para",
    "com", "em", "ao", "na", "no", "nas", "nos", "um", "uma", "que", "se",
    "por", "dia", "hoje", "sem", "nao", "nao", "tudo", "esta", "este",
    "foi", "ser", "tem", "hum", "esse", "essa", "isso", "ate", "ate",
}


def _normalize_key(text: str, max_chars: int = 40) -> str:
    """Normaliza texto para uma chave estavel usada em dedup de carryover.

    Heuristica: lowercase, remove pontuacao e stop-words, fica com primeiros
    tokens significativos ate max_chars de alfanumericos concatenados.
    """
    if not text:
        return ""
    words = [w.lower() for w in re.split(r"\s+", text) if w.strip()]
    significant = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    key_parts: list[str] = []
    for w in significant[:6]:
        key_parts.append("".join(c for c in w if c.isalnum()))
    key = "".join(key_parts)[:max_chars]
    if not key:
        key = "".join(c for c in text.lower() if c.isalnum())[:25]
    return key


def _dedup_by_key(items: list[Any], key_fn) -> tuple[list[Any], int]:
    """Dedup generico preservando ordem. Devolve (unique_items, dupes_removidos)."""
    seen: set[str] = set()
    out: list[Any] = []
    for it in items:
        try:
            k = key_fn(it)
        except Exception:
            k = repr(it)
        if not k:
            out.append(it)
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out, len(items) - len(out)


def _extract_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            t = prop.get("title") or []
            return ("".join(p.get("plain_text", "") for p in t).strip()) or "(sem titulo)"
    return "(sem titulo)"


def _extract_prop_str(page: dict, prop_name: str) -> str:
    p = page.get("properties", {}).get(prop_name, {})
    t = p.get("type")
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in p.get("rich_text", []))
    if t == "select":
        sel = p.get("select"); return sel.get("name", "") if sel else ""
    if t == "status":
        st = p.get("status"); return st.get("name", "") if st else ""
    if t == "date":
        d = p.get("date"); return d.get("start", "") if d else ""
    if t == "multi_select":
        return ", ".join(s.get("name", "") for s in p.get("multi_select", []))
    if t == "number":
        n = p.get("number"); return str(n) if n is not None else ""
    if t == "checkbox":
        return "sim" if p.get("checkbox") else "nao"
    if t == "people":
        return ", ".join(x.get("name", "") for x in p.get("people", []))
    return ""


def _fmt_page(page: dict, extra_props: list[str]) -> str:
    title = _extract_title(page)
    lines = [f"- {title}"]
    for p in extra_props:
        v = _extract_prop_str(page, p)
        if v:
            lines.append(f"    {p}: {v}")
    return "\n".join(lines)


async def _safe_query(notion: NotionClient, ds: str, **kw: Any) -> dict:
    try:
        return await notion.query_data_source(ds, **kw)
    except Exception as exc:
        log.warning("notion.query_failed", ds=ds, err=str(exc))
        return {"results": []}


async def _extract_carryover(notion: NotionClient) -> list[str]:
    """Le to_do blocks unchecked da pagina anterior + deduplica por chave estavel.

    v0.5.0: chave de normalizacao mais robusta (ver _normalize_key). Apanha
    variantes como "Verificar extractos bancarios Marco 2026" vs "Marco 2026:
    extractos bancarios por verificar" porque a chave cai em
    "verificarextractosbancariosmarco2026".
    """
    try:
        blocks = await notion.get_block_children(MORNING_BRIEFING_PAGE_ID)
    except Exception as exc:
        log.warning("carryover.read_failed", err=str(exc))
        return []

    unchecked: list[str] = []
    for b in blocks:
        if b.get("type") != "to_do":
            continue
        td = b.get("to_do", {})
        if td.get("checked"):
            continue
        rt = td.get("rich_text", []) or []
        text = "".join(r.get("plain_text", "") for r in rt).strip()
        if text:
            unchecked.append(text)

    deduped, dupes = _dedup_by_key(unchecked, _normalize_key)
    if dupes:
        log.info("carryover.dedup", antes=len(unchecked), depois=len(deduped), dupes=dupes)
    return deduped


def _build_email_table(email_stats: dict, drafts_by_account: dict) -> dict:
    header = [
        rt_array("Conta"), rt_array("Lidos"), rt_array("Faturas"),
        rt_array("Arquiv."), rt_array("Apagad."), rt_array("P/tratar"),
        rt_array("Manual"), rt_array("Rasc."),
    ]
    rows = [header]
    for acc in EMAIL_ACCOUNTS:
        s = email_stats.get(acc["account"], {})
        drafts_n = drafts_by_account.get(acc["account"], 0)
        rows.append([
            rt_array(acc["label"]),
            rt_array(str(s.get("read", "—"))),
            rt_array(str(s.get("invoices", "—"))),
            rt_array(str(s.get("archived", "—"))),
            rt_array(str(s.get("deleted", "—"))),
            rt_array(str(s.get("pending", "—"))),
            rt_array(str(s.get("manual_invoices", "—"))),
            rt_array(str(drafts_n) if drafts_n else "—"),
        ])
    return table(rows, has_column_header=True)


def _build_invoices_section(invoices: list[dict]) -> list[dict]:
    blocks: list[dict] = [heading_2("Faturas arquivadas hoje")]
    if not invoices:
        blocks.append(callout("Sem faturas arquivadas hoje.", emoji="📂"))
        return blocks

    by_company: dict[str, list[dict]] = {}
    total_amount: float = 0.0
    for inv in invoices:
        c = inv.get("company", "Outros")
        by_company.setdefault(c, []).append(inv)
        try:
            total_amount += float(inv.get("amount") or 0)
        except Exception:
            pass

    blocks.append(paragraph(
        f"{len(invoices)} fatura(s) arquivada(s) hoje. "
        f"Valor total estimado: {total_amount:.2f} EUR (aproximado)."
    ))

    for company in sorted(by_company.keys()):
        items = by_company[company]
        blocks.append(heading_2(f"{company} ({len(items)})"))
        header = [rt_array("Fornecedor"), rt_array("Data"),
                  rt_array("Valor"), rt_array("Trimestre"), rt_array("Ficheiro")]
        rows = [header]
        for inv in items:
            amount = inv.get("amount")
            cur = inv.get("currency", "EUR")
            amt_str = f"{float(amount):.2f} {cur}" if amount else "—"
            rows.append([
                rt_array(str(inv.get("supplier") or "—")),
                rt_array(str(inv.get("date") or "—")),
                rt_array(amt_str),
                rt_array(str(inv.get("quarter") or "—")),
                rt_array(str(inv.get("filename", ""))[-50:]),
            ])
        blocks.append(table(rows, has_column_header=True))
    return blocks


def _build_manual_invoices_section(manual: list[dict]) -> list[dict]:
    blocks: list[dict] = [heading_2("Faturas pendentes extracção manual")]
    if not manual:
        blocks.append(callout("Sem faturas para extracção manual.", emoji="✅"))
        return blocks
    blocks.append(paragraph(
        f"{len(manual)} email(s) classificados como fatura mas Carlos não conseguiu "
        f"extrair o PDF automaticamente. Abre cada um e arquiva manualmente."
    ))
    for m in manual[:30]:
        label = f"→ {m.get('subject', '(sem assunto)')[:80]} | {m.get('from', '')[:40]} | {m.get('inbox', '')}"
        url = m.get("url", "")
        if url:
            blocks.append(paragraph_link(label, url))
        else:
            blocks.append(paragraph(label))
        if m.get("reason"):
            blocks.append(callout(f"Razão: {m['reason']}", emoji="⚠️"))
    return blocks


def _build_drafts_section(drafts: list[dict]) -> list[dict]:
    blocks: list[dict] = [heading_2("Rascunhos de resposta")]
    if not drafts:
        blocks.append(callout("Sem rascunhos pendentes.", emoji="✉️"))
        return blocks
    for d in drafts[:25]:
        subj = d.get("subject", "(sem assunto)")
        from_addr = d.get("from_addr", "?")
        account = d.get("account", "?")
        url = d.get("url", "")
        label = f"→ {subj} | De: {from_addr} | Caixa: {account}"
        if url:
            blocks.append(paragraph_link(label, url))
        else:
            blocks.append(paragraph(label))
        preview = d.get("preview", "")
        if preview:
            blocks.append(callout(preview[:300], emoji="📝"))
    return blocks


def _draft_key(d: dict) -> str:
    """Chave de dedup de draft: thread_id se existir; senao URL; senao (acc|subject)."""
    thread = d.get("thread_id") or d.get("threadId") or ""
    if thread:
        return f"thread:{thread}"
    url = d.get("url") or ""
    if url:
        return f"url:{url}"
    acc = d.get("account") or ""
    subj = (d.get("subject") or "").lower().strip()
    return f"acc:{acc}|subj:{subj[:80]}"


def _pending_key(p: dict) -> str:
    """Chave de dedup de pending_email: token (unico por mensagem real)."""
    tok = p.get("token") or ""
    if tok:
        return f"tok:{tok}"
    # Fallback: account + message_id
    return f"acc:{p.get('account','')}|mid:{p.get('message_id','') or p.get('id','')}"


SYSTEM_PROMPT = """És o Carlos, Chief of Staff e Dispatcher da OMNAI. Compões todas as manhãs o briefing executivo para o David Sardinha, CEO, que gere 4 empresas (OMNAI, Previnsa, JMSoares, Sopato).

Regras de estilo:
- Português de Portugal, directo, sem clichés
- Sem travessões "—"; usa vírgulas, ponto e vírgula ou frase nova
- Sem "não é sobre X, é sobre Y"
- Frases curtas com verbo

Formato (markdown, secções por esta ordem):
## Sumário
2 a 3 frases sobre estado geral.

## Prioridades do dia
"- [ ] Verbo + contexto curto". Até 5 itens.
IMPORTANTE: se te for dado um bloco "ITENS JA EM CARRYOVER (NAO REPETIR)",
NAO incluas itens que cubram esses tópicos. Isso evita duplicacao entre
Prioridades e Pendente dos dias anteriores.

## Alertas e deadlines
"- [ ] texto" se accionável; "- texto" se informativo.

## Pipeline e vendas
Prosa curta. Bullets opcionais ("- ").

Máximo 400 palavras."""


async def run() -> dict[str, Any]:
    started = datetime.now(timezone.utc)

    async with NotionClient() as notion:
        carryover = await _extract_carryover(notion)

        activity = await _safe_query(
            notion, ACTIVITY_LOG_DB,
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=20,
        )
        tasks = await _safe_query(notion, TAREFAS_DB, page_size=20)
        pipeline = await _safe_query(notion, PIPELINE_DB, page_size=15)
        contratos = await _safe_query(notion, CONTRATOS_DB, page_size=10)
        decisoes = await _safe_query(
            notion, DECISOES_DB,
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=5,
        )

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
            pending_emails_raw = await peek_pending_emails()
        except Exception:
            pending_emails_raw = []

        # v0.5.0: dedup endurecido de listas de fila antes de renderizar
        drafts, drafts_dupes = _dedup_by_key(drafts_raw, _draft_key)
        pending_emails, pending_dupes = _dedup_by_key(pending_emails_raw, _pending_key)
        invoices, invoices_dupes = _dedup_by_key(
            invoices, lambda x: x.get("filename") or x.get("message_id") or ""
        )
        manual_invoices, manual_dupes = _dedup_by_key(
            manual_invoices, lambda x: x.get("url") or x.get("message_id") or ""
        )

        if drafts_dupes or pending_dupes or invoices_dupes or manual_dupes:
            log.info(
                "briefing.dedup_queues",
                drafts=drafts_dupes,
                pending=pending_dupes,
                invoices=invoices_dupes,
                manual=manual_dupes,
            )

        drafts_by_account: dict[str, int] = {}
        for d in drafts:
            acc = d.get("account", "")
            drafts_by_account[acc] = drafts_by_account.get(acc, 0) + 1

        # Contexto Claude
        ctx: list[str] = []
        ctx.append("## Actividades recentes")
        if activity.get("results"):
            for p in activity["results"][:15]:
                ctx.append(_fmt_page(p, ["Empresa", "Agente", "Tipo"]))
        else:
            ctx.append("(sem actividade)")

        ctx.append("\n## Tarefas abertas")
        if tasks.get("results"):
            for p in tasks["results"][:15]:
                ctx.append(_fmt_page(p, ["Status", "Deadline", "Empresa", "Prioridade"]))
        else:
            ctx.append("(vazio)")

        ctx.append("\n## Pipeline comercial")
        if pipeline.get("results"):
            for p in pipeline["results"][:10]:
                ctx.append(_fmt_page(p, ["Stage", "Valor", "Empresa", "Probabilidade"]))
        else:
            ctx.append("(vazio)")

        ctx.append("\n## Contratos e obrigacoes")
        if contratos.get("results"):
            for p in contratos["results"][:8]:
                ctx.append(_fmt_page(p, ["Tipo", "Data limite", "Empresa"]))
        else:
            ctx.append("(vazio)")

        ctx.append("\n## Decisoes recentes")
        if decisoes.get("results"):
            for p in decisoes["results"][:5]:
                ctx.append(_fmt_page(p, ["Contexto", "Empresa"]))
        else:
            ctx.append("(vazio)")

        # v0.5.0: passar carryover ao LLM como lista de NAO-REPETIR
        if carryover:
            ctx.append("\n## ITENS JA EM CARRYOVER (NAO REPETIR em Prioridades do dia)")
            for item in carryover[:20]:
                ctx.append(f"- {item[:200]}")

        if invoices:
            ctx.append(f"\n## Faturas arquivadas hoje: {len(invoices)}")
        if manual_invoices:
            ctx.append(f"\n## Faturas pendentes extraccao manual: {len(manual_invoices)}")
        if drafts:
            ctx.append(f"\n## Rascunhos de resposta criados: {len(drafts)}")

        context = "\n".join(ctx)
        today_label = started.strftime("%A, %d de %B de %Y")

        try:
            briefing_md = await generate(
                system=SYSTEM_PROMPT,
                prompt=f"Dados da OMNAI ({today_label}):\n\n{context}\n\nCompõe o briefing.",
                max_tokens=1800,
            )
        except Exception as exc:
            log.exception("briefing.generate_failed")
            return {"status": "error", "stage": "llm", "error": f"{type(exc).__name__}: {exc}"}

        blocks: list[dict] = [
            heading_1(f"Briefing {today_label}"),
            callout(f"Gerado pelo Carlos às {started.strftime('%H:%M')} UTC.", emoji="🌅"),
            divider(),
        ]

        blocks.append(heading_2("Pendente dos dias anteriores"))
        if carryover:
            for item in carryover[:30]:
                blocks.append(to_do(item, checked=False))
        else:
            blocks.append(paragraph("Sem itens transitados."))

        blocks.append(divider())
        blocks.extend(markdown_to_blocks(briefing_md))
        blocks.append(divider())

        # Tabela email
        blocks.append(heading_2("Estado das caixas de correio"))
        blocks.append(paragraph("Previnsa é tratada via opaidapetinga@gmail.com (forwarding)."))
        blocks.append(_build_email_table(email_stats, drafts_by_account))
        blocks.append(divider())

        # Faturas arquivadas hoje
        blocks.extend(_build_invoices_section(invoices))
        blocks.append(divider())

        # Faturas pendentes manual
        blocks.extend(_build_manual_invoices_section(manual_invoices))
        blocks.append(divider())

        # Emails pendentes de accao (checkbox -> auto-archive na proxima corrida)
        if pending_emails:
            blocks.append(heading_2("📧 Emails pendentes de acção (marca ✅ para arquivar)"))
            blocks.append(paragraph(
                "Marca o checkbox nos que queres arquivar. Na próxima corrida "
                "(midday/evening/morning) o Carlos arquiva-os automaticamente nas "
                "pastas correctas das respectivas contas."
            ))
            for p in pending_emails[:40]:
                subj = (p.get("subject") or "(sem assunto)")[:120]
                frm = (p.get("from") or "?")[:60]
                title = f"{subj} — de {frm}"
                url = MAIL_TOKEN_PREFIX + (p.get("token") or "")
                blocks.append({
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": title[:2000], "link": {"url": url}},
                        }],
                        "checked": False,
                    },
                })
            blocks.append(divider())

        # Drafts
        blocks.extend(_build_drafts_section(drafts))
        blocks.append(divider())

        blocks.append(paragraph(
            "Fontes: Activity Log ({a}), Tarefas ({t}), Pipeline ({p}), Contratos ({c}), "
            "Decisões ({d}). Carry-over: {co}. Faturas: {fa} | Manual: {fm} | Drafts: {dr} | "
            "Dedup removido: pending={pd}, drafts={dd}.".format(
                a=len(activity.get("results", [])),
                t=len(tasks.get("results", [])),
                p=len(pipeline.get("results", [])),
                c=len(contratos.get("results", [])),
                d=len(decisoes.get("results", [])),
                co=len(carryover), fa=len(invoices), fm=len(manual_invoices),
                dr=len(drafts), pd=pending_dupes, dd=drafts_dupes,
            )
        ))

        try:
            await notion.replace_page_content(MORNING_BRIEFING_PAGE_ID, blocks)
        except Exception as exc:
            log.exception("briefing.publish_failed")
            return {"status": "error", "stage": "publish",
                    "error": f"{type(exc).__name__}: {exc}",
                    "briefing_preview": briefing_md[:500]}

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "status": "ok",
            "briefing_chars": len(briefing_md),
            "page_id": MORNING_BRIEFING_PAGE_ID,
            "carryover_items": len(carryover),
            "invoices_today": len(invoices),
            "manual_invoices": len(manual_invoices),
            "drafts_pending": len(drafts),
            "pending_emails": len(pending_emails),
            "dedup_removed": {
                "pending": pending_dupes,
                "drafts": drafts_dupes,
                "invoices": invoices_dupes,
                "manual": manual_dupes,
            },
            "email_stats_accounts": len(email_stats),
            "elapsed_s": round(elapsed, 2),
        }
