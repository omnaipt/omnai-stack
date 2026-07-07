"""Worker email-scan v0.6.0 (Sprint 5).

Fluxo por cada email:
  classify -> {actionable, invoice, archive, delete, keep}

  actionable -> draft no Gmail (ou texto p/ IMAP), push_draft, deixa em inbox
  invoice    -> tentar extract_pdf, save_invoice, archive email; se falhar -> manual queue
  archive    -> remover label INBOX (Gmail) ou move Archive (IMAP)
  delete     -> trash (apenas se SCAN_MODE=aggressive)
  keep       -> nao mexe

Sprint 5: emite cards em briefing_items para itens accionaveis e faturas
processadas/falhadas, sem mexer no resto do pipeline.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import structlog

from services import gmail, imap_client
from services.briefing_emit import emit_briefing
from services.classifier import classify, extract_email
from services.drafter import draft_response
from services import email_inbox_db
from services.invoices import (
    company_for_inbox,
    extract_metadata,
    save_invoice_pdf,
)
from services.state import (
    push_pending_email,
    push_draft, push_invoice, push_manual_invoice, set_email_stats,
)

log = structlog.get_logger()


import base64 as _b64

WORKER_NAME = "email-scan"


def _mk_pending_token(account: str, msg_id: str) -> str:
    raw = f"{account}|{msg_id}".encode("utf-8")
    return _b64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_pending_token(token: str) -> tuple[str, str] | None:
    try:
        pad = "=" * ((4 - len(token) % 4) % 4)
        raw = _b64.urlsafe_b64decode(token + pad).decode("utf-8")
        if "|" in raw:
            acc, mid = raw.split("|", 1)
            return acc, mid
    except Exception:
        pass
    return None


async def _push_pending(account: str, msg_id: str, subject: str, from_addr: str, reason: str) -> None:
    token = _mk_pending_token(account, msg_id)
    await push_pending_email({
        "account": account,
        "msg_id": msg_id,
        "token": token,
        "subject": (subject or "")[:200],
        "from": (from_addr or "")[:150],
        "reason": (reason or "")[:150],
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


SCAN_MODE = os.getenv("SCAN_MODE", "balanced").lower()
SCAN_DRY_RUN = os.getenv("SCAN_DRY_RUN", "false").lower() in ("1", "true", "yes")
SCAN_CAP = int(os.getenv("SCAN_CAP", "50"))


GMAIL_ACCOUNTS = list(gmail.GMAIL_ACCOUNTS.keys())
IMAP_ACCOUNTS = [
    "hello@omnai.pt",
    "david.sardinha@omnai.pt",
    "david.sardinha@sapo.pt",
]
VIRTUAL_ROUTES = {"david.sardinha@previnsa.com": "opaidapetinga@gmail.com"}


def _mode_policy() -> dict[str, bool]:
    return {
        "strict":     {"archive": False, "delete": False, "draft": True, "invoice": True},
        "balanced":   {"archive": True,  "delete": False, "draft": True, "invoice": True},
        "aggressive": {"archive": True,  "delete": True,  "draft": True, "invoice": True},
    }.get(SCAN_MODE, {"archive": True, "delete": False, "draft": True, "invoice": True})


def _zero_stats() -> dict[str, int]:
    return {"read": 0, "archived": 0, "deleted": 0, "pending": 0,
            "kept": 0, "invoices": 0, "manual_invoices": 0}


def _to_thread(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


def _empresa_para_card(account: str) -> str:
    """Mapeia o inbox para o conjunto canonico de briefing_db.EMPRESAS.

    Usa o mesmo dicionario que o services/invoices.py (INBOX_TO_COMPANY)
    via company_for_inbox. Caso retorne 'Outros', cai para 'OMNAI' por
    ser o default mais comum em emails recebidos pelo David.
    """
    company = company_for_inbox(account)
    if company in ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal"):
        return company
    return "OMNAI"


# -----------------------------------------------------------------------
# Gmail processing
# -----------------------------------------------------------------------

async def _process_invoice_gmail(account: str, summary: dict) -> tuple[bool, str, dict | None]:
    """Tenta extrair fatura do email Gmail. Retorna (sucesso, motivo, primeiro_saved)."""
    atts = summary.get("attachments", []) or []
    pdf_atts = [a for a in atts if a.get("filename", "").lower().endswith(".pdf")]
    if not pdf_atts:
        return False, "sem PDF em anexo", None

    success_any = False
    first_saved: dict | None = None
    for att in pdf_atts[:3]:
        try:
            pdf_bytes = await _to_thread(
                gmail.download_attachment, account, summary["id"], att["attachment_id"]
            )
        except Exception as exc:
            log.warning("gmail.attachment_download_failed", err=str(exc))
            continue

        meta = await extract_metadata(
            pdf_bytes,
            hint_from=summary.get("from", ""),
            hint_subject=summary.get("subject", ""),
        )
        if meta.get("error") and not (meta.get("supplier") or meta.get("date")):
            log.warning("invoice.metadata_failed", err=meta.get("error"))
            return False, f"extracção falhou: {meta.get('error')}", None

        try:
            saved = await _to_thread(save_invoice_pdf, pdf_bytes, meta, account)
        except Exception as exc:
            log.warning("invoice.save_failed", err=str(exc))
            return False, f"save: {exc}", None

        await push_invoice({
            **saved,
            "inbox": account,
            "email_subject": summary.get("subject", ""),
            "email_from": summary.get("from", ""),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        })
        success_any = True
        if first_saved is None:
            first_saved = saved

    return success_any, "ok" if success_any else "nenhum PDF processavel", first_saved


async def _process_invoice_imap(account: str, msg: dict) -> tuple[bool, str, dict | None]:
    try:
        atts = await _to_thread(imap_client.download_attachments, account, msg["id"])
    except Exception as exc:
        return False, f"download: {exc}", None

    pdf_atts = [(n, b) for n, b in atts if n.lower().endswith(".pdf")]
    if not pdf_atts:
        return False, "sem PDF anexo", None

    success_any = False
    first_saved: dict | None = None
    for filename, pdf_bytes in pdf_atts[:3]:
        meta = await extract_metadata(
            pdf_bytes,
            hint_from=msg.get("from", ""),
            hint_subject=msg.get("subject", ""),
        )
        if meta.get("error") and not (meta.get("supplier") or meta.get("date")):
            continue
        try:
            saved = await _to_thread(save_invoice_pdf, pdf_bytes, meta, account)
        except Exception as exc:
            log.warning("invoice.save_failed", err=str(exc))
            continue
        await push_invoice({
            **saved,
            "inbox": account,
            "email_subject": msg.get("subject", ""),
            "email_from": msg.get("from", ""),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        })
        success_any = True
        if first_saved is None:
            first_saved = saved

    return success_any, "ok" if success_any else "nenhum PDF processavel", first_saved


async def _create_gmail_draft_for(account: str, summary: dict, reason: str) -> None:
    from_email = extract_email(summary.get("from", ""))
    subj = summary.get("subject", "")
    reply_subj = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    try:
        body = await draft_response(account, summary.get("from", ""), subj, summary.get("body", ""))
    except Exception as exc:
        log.warning("drafter.failed", err=str(exc))
        return
    try:
        draft = await _to_thread(
            gmail.create_draft, account, from_email, reply_subj, body,
            summary.get("thread_id"), summary.get("message_id_header"),
        )
        url = f"https://mail.google.com/mail/u/0/#drafts/{draft.get('id', '')}" if draft.get('id') else ""
        draft_id = draft.get("id", "")
    except Exception as exc:
        log.warning("gmail.draft_failed", err=str(exc))
        draft_id = f"failed-{summary.get('id', '')}"
        url = ""
    await push_draft({
        "draft_id": draft_id,
        "subject": reply_subj,
        "from_addr": from_email,
        "account": account,
        "preview": body[:250],
        "full_text": body,
        "url": url,
        "classification_reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


async def _emit_actionable_card(account: str, msg_id: str, subject: str, from_addr: str, reason: str) -> None:
    titulo = (subject or "(sem assunto)")[:180]
    detalhe = f"De: {from_addr or '(desconhecido)'}\nMotivo classificador: {reason or '-'}"
    # v9.7: persist no email_inbox para a tab Emails da PWA
    try:
        await email_inbox_db.upsert(
            account=account,
            message_id=msg_id,
            thread_id=None,
            from_addr=from_addr or "",
            subject=subject or "",
            snippet=(reason or "")[:500],
            classificacao="actionable",
            gmail_thread_url=gmail.get_message_url(account, msg_id) if hasattr(gmail, "get_message_url") else None,
        )
    except Exception as exc:
        log.warning("email_inbox.upsert_failed", err=str(exc))
    await emit_briefing(
        tipo="email_actionable",
        titulo=titulo,
        detalhe=detalhe,
        urgencia="P1",
        empresa=_empresa_para_card(account),
        chave_parts=(msg_id,),
        link_origem=gmail.get_message_url(account, msg_id),
        metadata={
            "gmail_message_id": msg_id,
            "gmail_inbox": account,
            "account": account,
            "from": (from_addr or "")[:200],
            "subject": (subject or "")[:200],
            "reason": (reason or "")[:200],
        },
        worker_name=WORKER_NAME,
    )


async def _emit_invoice_pendente_card(account: str, msg_id: str, subject: str, from_addr: str, reason: str, link: str) -> None:
    titulo = f"Fatura por extrair: {subject or '(sem assunto)'}"[:180]
    detalhe = f"De: {from_addr or '(desconhecido)'}\nMotivo: {reason or '-'}"
    await emit_briefing(
        tipo="email_fatura_pendente",
        titulo=titulo,
        detalhe=detalhe,
        urgencia="P1",
        empresa=_empresa_para_card(account),
        chave_parts=(msg_id, "fatura"),
        link_origem=link or gmail.get_message_url(account, msg_id),
        metadata={
            "gmail_message_id": msg_id,
            "gmail_inbox": account,
            "account": account,
            "from": (from_addr or "")[:200],
            "subject": (subject or "")[:200],
            "reason": (reason or "")[:200],
            "company": company_for_inbox(account),
        },
        worker_name=WORKER_NAME,
    )


async def process_gmail(account: str) -> dict[str, Any]:
    stats = _zero_stats()
    sample: list[dict] = []
    policy = _mode_policy()

    try:
        listing = await _to_thread(gmail.list_inbox_messages, account, 24, SCAN_CAP)
    except Exception as exc:
        log.warning("gmail.list_failed", account=account, err=str(exc))
        return {**stats, "sample": sample}

    for item in listing:
        try:
            summary = await _to_thread(gmail.summarize_message, account, item["id"])
            if not summary:
                continue
            stats["read"] += 1

            cls = await classify(summary.get("from", ""), summary.get("subject", ""), summary.get("body", ""), headers=summary.get("headers") or {}, to_addr=account)
            classification = cls.get("classification", "keep")
            reason = cls.get("reason", "")

            sample.append({
                "from": summary.get("from", "")[:60],
                "subject": summary.get("subject", "")[:60],
                "class": classification,
                "reason": reason[:100],
            })

            if classification == "actionable":
                stats["pending"] += 1
                await _push_pending(account, summary["id"], summary.get("subject",""), summary.get("from",""), reason)
                await _emit_actionable_card(
                    account, summary["id"],
                    summary.get("subject", ""), summary.get("from", ""), reason,
                )
                if not (SCAN_DRY_RUN or not policy["draft"]):
                    # v9.7: draft on-demand via PWA, nao gerado automaticamente

                    log.info("draft.skipped", reason="on_demand_via_pwa", account=account)

            elif classification == "invoice":
                if SCAN_DRY_RUN or not policy["invoice"]:
                    stats["kept"] += 1
                    continue
                success, msg, _saved = await _process_invoice_gmail(account, summary)
                if success:
                    stats["invoices"] += 1
                    if policy["archive"]:
                        await _to_thread(gmail.archive_message, account, summary["id"])
                        stats["archived"] += 1
                else:
                    stats["manual_invoices"] += 1
                    msg_url = gmail.get_message_url(account, summary["id"])
                    await push_manual_invoice({
                        "inbox": account,
                        "from": summary.get("from", ""),
                        "subject": summary.get("subject", ""),
                        "url": msg_url,
                        "company": company_for_inbox(account),
                        "reason": msg,
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                    })
                    await _emit_invoice_pendente_card(
                        account, summary["id"],
                        summary.get("subject", ""), summary.get("from", ""),
                        msg, msg_url,
                    )

            elif classification == "archive":
                if SCAN_DRY_RUN or not policy["archive"]:
                    stats["kept"] += 1
                    continue
                ok = await _to_thread(gmail.archive_message, account, summary["id"])
                stats["archived" if ok else "kept"] += 1

            elif classification == "delete":
                is_heur = reason.startswith("heuristic.")
                can_delete = policy["delete"] or is_heur
                if SCAN_DRY_RUN or not can_delete:
                    stats["kept"] += 1
                    continue
                ok = await _to_thread(gmail.trash_message, account, summary["id"])
                stats["deleted" if ok else "kept"] += 1
            else:
                stats["kept"] += 1

        except Exception as exc:
            log.warning("gmail.process_one_failed", err=str(exc))
            continue

    return {**stats, "sample": sample[-15:]}


async def process_imap(account: str) -> dict[str, Any]:
    stats = _zero_stats()
    sample: list[dict] = []
    policy = _mode_policy()

    try:
        msgs = await _to_thread(imap_client.list_inbox_messages, account, 48, SCAN_CAP)
    except Exception as exc:
        log.warning("imap.list_failed", account=account, err=str(exc))
        return {**stats, "sample": sample}

    for m in msgs:
        try:
            body = await _to_thread(imap_client.get_message_body, account, m["id"])
            stats["read"] += 1

            cls = await classify(m.get("from", ""), m.get("subject", ""), body, headers=m.get("headers") or {}, to_addr=account)
            classification = cls.get("classification", "keep")
            reason = cls.get("reason", "")

            sample.append({
                "from": m.get("from", "")[:60],
                "subject": m.get("subject", "")[:60],
                "class": classification,
                "reason": reason[:100],
            })

            if classification == "actionable":
                stats["pending"] += 1
                await _push_pending(account, m["id"], m.get("subject",""), m.get("from",""), reason)
                await _emit_actionable_card(
                    account, m["id"],
                    m.get("subject", ""), m.get("from", ""), reason,
                )
                if not (SCAN_DRY_RUN or not policy["draft"]):
                    try:
                        body_draft = await draft_response(
                            account, m.get("from", ""), m.get("subject", ""), body
                        )
                    except Exception:
                        continue
                    await push_draft({
                        "draft_id": f"imap-{account}-{m['id']}",
                        "subject": m.get("subject", "") if m.get("subject", "").lower().startswith("re:") else f"Re: {m.get('subject', '')}",
                        "from_addr": extract_email(m.get("from", "")),
                        "account": account,
                        "preview": body_draft[:250],
                        "full_text": body_draft,
                        "url": "",
                        "classification_reason": reason,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })

            elif classification == "invoice":
                if SCAN_DRY_RUN or not policy["invoice"]:
                    stats["kept"] += 1
                    continue
                success, msg_reason, _saved = await _process_invoice_imap(account, m)
                if success:
                    stats["invoices"] += 1
                    if policy["archive"]:
                        await _to_thread(imap_client.archive_message, account, m["id"])
                        stats["archived"] += 1
                else:
                    stats["manual_invoices"] += 1
                    await push_manual_invoice({
                        "inbox": account,
                        "from": m.get("from", ""),
                        "subject": m.get("subject", ""),
                        "url": "",
                        "company": company_for_inbox(account),
                        "reason": msg_reason,
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                    })
                    await _emit_invoice_pendente_card(
                        account, m["id"],
                        m.get("subject", ""), m.get("from", ""),
                        msg_reason, "",
                    )

            elif classification == "archive":
                if SCAN_DRY_RUN or not policy["archive"]:
                    stats["kept"] += 1
                    continue
                ok = await _to_thread(imap_client.archive_message, account, m["id"])
                stats["archived" if ok else "kept"] += 1

            elif classification == "delete":
                is_heur = reason.startswith("heuristic.")
                can_delete = policy["delete"] or is_heur
                if SCAN_DRY_RUN or not can_delete:
                    stats["kept"] += 1
                    continue
                ok = await _to_thread(imap_client.delete_message, account, m["id"])
                stats["deleted" if ok else "kept"] += 1
            else:
                stats["kept"] += 1

        except Exception as exc:
            log.warning("imap.process_one_failed", err=str(exc))
            continue

    return {**stats, "sample": sample[-15:]}


async def run() -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    results: dict[str, Any] = {}

    for acc in GMAIL_ACCOUNTS:
        r = await process_gmail(acc)
        await set_email_stats(acc, {k: v for k, v in r.items() if k not in ("sample",)})
        results[acc] = r

    for acc in IMAP_ACCOUNTS:
        r = await process_imap(acc)
        await set_email_stats(acc, {k: v for k, v in r.items() if k not in ("sample",)})
        results[acc] = r

    for virt, real in VIRTUAL_ROUTES.items():
        src = results.get(real, {**_zero_stats(), "sample": []})
        payload = {k: src.get(k, 0) for k in _zero_stats().keys()}
        await set_email_stats(virt, payload)
        results[virt] = {**payload, "note": f"via {real}"}

    await set_email_stats("david.sardinha@jmsoares.pt", _zero_stats())
    results["david.sardinha@jmsoares.pt"] = {**_zero_stats(), "note": "sem integracao"}

    keys = ("read", "archived", "deleted", "pending", "kept", "invoices", "manual_invoices")
    totals = {k: sum(r.get(k, 0) for r in results.values() if isinstance(r, dict)) for k in keys}
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "status": "ok",
        "mode": SCAN_MODE,
        "dry_run": SCAN_DRY_RUN,
        "cap": SCAN_CAP,
        "accounts_processed": len(results),
        "totals": totals,
        "per_account": results,
        "elapsed_s": round(elapsed, 2),
        "ts": started.isoformat(),
    }
