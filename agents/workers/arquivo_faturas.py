"""Worker: arquivo-faturas | Ana | CFO

Cron: Domingo 20:00 UTC.
Relatorio semanal complementando o real-time archive do Carlos.

Sprint 5: emite cards em briefing_items por:
  * fatura arquivada na ultima semana (P3 informativo)  ->  tipo `fatura_arquivada`
  * entrada na manual_invoices queue (P1 accionavel)    ->  tipo `fatura_sem_documento`

Sprint 7.5: filtra por responsabilidade contabilistica do David. Cards
fatura_arquivada e fatura_sem_documento so para OMNAI, Sopato e Pessoal.
A pagina de morning briefing, contagem global e activity log mantem-se
inalterados (David ve no briefing as contagens de TODAS as empresas, mas
nao recebe cards individuais para Previnsa/JMSoares).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import structlog

from services import notion_ext
from services.briefing_emit import emit_briefing
from services.notion_ext import heading, paragraph, bullet
from services.responsabilidade_david import is_alerta_relevante
from services.state import peek_invoices, peek_manual_invoices

log = structlog.get_logger()


FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"

WORKER_NAME = "arquivo-faturas"
TIPO_ARQUIVADA = "fatura_arquivada"
TIPO_MANUAL = "fatura_sem_documento"

EMPRESAS = ["OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal"]
EMPRESAS_CANONICAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")


def _semana() -> tuple[int, int]:
    ano, semana, _ = date.today().isocalendar()
    return ano, semana


def _normalizar_empresa(raw: str) -> str:
    s = (raw or "").strip()
    return s if s in EMPRESAS_CANONICAS else "OMNAI"


def _contar_recentes() -> dict[str, int]:
    agora = date.today()
    seven = agora - timedelta(days=7)
    out: dict[str, int] = defaultdict(int)
    for emp in EMPRESAS:
        raiz = FATURAS_ROOT / emp
        if not raiz.exists():
            continue
        for pdf in raiz.rglob("*.pdf"):
            try:
                mtime = date.fromtimestamp(pdf.stat().st_mtime)
                if mtime >= seven:
                    out[emp] += 1
            except Exception:
                continue
    return dict(out)


def _parse_entry(entry) -> dict:
    if isinstance(entry, dict):
        return entry
    try:
        return json.loads(entry)
    except Exception:
        return {}


async def _emitir_cards_arquivadas(arquivadas: list) -> int:
    """1 card P3 por fatura arquivada na ultima semana.

    Chave (invoice_number,) ou (filename,) como fallback. Idempotente: se
    a mesma fatura aparecer em runs sucessivos do peek, fica deduplicada.

    Sprint 7.5: filtra por is_alerta_relevante.
    """
    n = 0
    skipped_fora_escopo = 0
    for raw in arquivadas:
        obj = _parse_entry(raw)
        if not obj:
            continue
        invoice_num = obj.get("invoice_number")
        filename = obj.get("filename") or ""
        if not invoice_num and not filename:
            continue
        chave_id = str(invoice_num) if invoice_num else filename
        empresa = _normalizar_empresa(obj.get("company"))
        if not is_alerta_relevante(tipo=TIPO_ARQUIVADA, empresa=empresa):
            skipped_fora_escopo += 1
            log.debug(
                "skip emit_briefing",
                tipo=TIPO_ARQUIVADA,
                empresa=empresa,
                reason="fora_responsabilidade_david",
            )
            continue
        supplier = obj.get("supplier") or "(fornecedor desconhecido)"
        amount = obj.get("amount")
        currency = obj.get("currency") or "EUR"
        invoice_date = obj.get("date") or ""
        try:
            amount_str = f"{float(amount):.2f} {currency}" if amount is not None else "(sem montante)"
        except Exception:
            amount_str = f"{amount} {currency}"
        titulo = f"Fatura arquivada: {supplier} | {amount_str} | {invoice_date}"
        detalhe = (
            f"Empresa: {empresa}\n"
            f"Fornecedor: {supplier}\n"
            f"Valor: {amount_str}\n"
            f"Data: {invoice_date or '-'}\n"
            f"Numero: {invoice_num or '-'}\n"
            f"Ficheiro: {filename or '-'}"
        )
        link = obj.get("drive_url") or None
        ok = await emit_briefing(
            tipo=TIPO_ARQUIVADA,
            titulo=titulo[:180],
            detalhe=detalhe,
            urgencia="P3",
            empresa=empresa,
            chave_parts=(chave_id,),
            link_origem=link,
            metadata={
                "company": empresa,
                "supplier": supplier,
                "amount": amount,
                "currency": currency,
                "date": invoice_date,
                "invoice_number": invoice_num,
                "filename": filename,
                "path": obj.get("path"),
                "drive_id": obj.get("drive_id"),
            },
            worker_name=WORKER_NAME,
        )
        if ok:
            n += 1
    if skipped_fora_escopo:
        log.info(
            "arquivo-faturas skipped fora-escopo",
            tipo=TIPO_ARQUIVADA,
            count=skipped_fora_escopo,
        )
    return n


async def _emitir_cards_manual(manual: list) -> int:
    """1 card P1 por entrada na manual queue.

    Sem invoice_number garantido; usa subject + from + inbox como chave
    composta para evitar dupes em runs sucessivos.

    Sprint 7.5: filtra por is_alerta_relevante.
    """
    n = 0
    skipped_fora_escopo = 0
    for raw in manual:
        obj = _parse_entry(raw)
        if not obj:
            continue
        subject = (obj.get("subject") or "").strip()
        from_addr = (obj.get("from") or "").strip()
        inbox = (obj.get("inbox") or "").strip()
        url = obj.get("url") or None
        # 07-08-2026: pelo assunto, "Order Received" e "Order Finished" da
        # mesma encomenda davam dois cartoes. Agrupa-se pela encomenda.
        from services.referencia_compra import chave_compra
        chave_id = chave_compra(
            subject, from_addr,
            fallback=subject or from_addr or inbox or "manual-fatura")
        empresa = _normalizar_empresa(obj.get("company"))
        if not is_alerta_relevante(tipo=TIPO_MANUAL, empresa=empresa):
            skipped_fora_escopo += 1
            log.debug(
                "skip emit_briefing",
                tipo=TIPO_MANUAL,
                empresa=empresa,
                reason="fora_responsabilidade_david",
            )
            continue
        titulo = f"Fatura sem documento processavel: {subject or '(sem assunto)'}"
        detalhe = (
            f"Empresa: {empresa}\n"
            f"Inbox: {inbox or '-'}\n"
            f"De: {from_addr or '-'}\n"
            f"Motivo: {obj.get('reason') or '-'}\n"
            "Accao: extrair manualmente o PDF e arquivar."
        )
        ok = await emit_briefing(
            tipo=TIPO_MANUAL,
            titulo=titulo[:180],
            detalhe=detalhe,
            urgencia="P1",
            empresa=empresa,
            chave_parts=(chave_id, "missing"),
            link_origem=url,
            metadata={
                "company": empresa,
                "inbox": inbox,
                "from": from_addr,
                "subject": subject,
                "reason": obj.get("reason"),
                "logged_at": obj.get("logged_at"),
            },
            worker_name=WORKER_NAME,
        )
        if ok:
            n += 1
    if skipped_fora_escopo:
        log.info(
            "arquivo-faturas skipped fora-escopo",
            tipo=TIPO_MANUAL,
            count=skipped_fora_escopo,
        )
    return n


async def run() -> dict:
    ano, semana = _semana()
    contagem = _contar_recentes()

    arquivadas = await peek_invoices()  # lista de dicts {subject, company, path, ...}
    manual = await peek_manual_invoices()
    total_fs = sum(contagem.values())

    blocks = [
        heading(2, f"Arquivo Faturas S{semana}/{ano}"),
        paragraph(
            f"{total_fs} faturas no filesystem nos ultimos 7 dias. "
            f"{len(arquivadas)} registadas hoje em Redis. {len(manual)} em manual queue."
        ),
        heading(3, "Por empresa (filesystem)"),
    ]
    for emp in EMPRESAS:
        blocks.append(bullet(f"{emp}: {contagem.get(emp, 0)} faturas"))

    if manual:
        blocks.append(heading(3, "Pendentes de extraccao manual"))
        for entry in manual[:20]:
            try:
                obj = json.loads(entry) if not isinstance(entry, dict) else entry
                desc = obj.get("subject") or obj.get("from") or "(sem subject)"
            except Exception:
                desc = str(entry)[:200]
            blocks.append(bullet(desc[:200]))
        if len(manual) > 20:
            blocks.append(paragraph(f"(+{len(manual) - 20} mais na queue)"))

    try:
        await notion_ext.append_children(MORNING_BRIEFING_PAGE, blocks)
    except Exception as exc:
        log.error("arquivo-faturas append briefing FAIL", err=str(exc))

    cards_arquivadas = await _emitir_cards_arquivadas(arquivadas)
    cards_manual = await _emitir_cards_manual(manual)

    await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=(
            f"Relatorio semanal faturas S{semana}/{ano} | {total_fs} fs | "
            f"{len(manual)} manual | {cards_arquivadas+cards_manual} cards"
        ),
    )

    out = {
        "status": "ok",
        "semana": f"S{semana}/{ano}",
        "contagem": contagem,
        "arquivadas_hoje": len(arquivadas),
        "manual_queue": len(manual),
        "cards_arquivadas": cards_arquivadas,
        "cards_manual": cards_manual,
    }
    log.info("arquivo-faturas", **out)
    return out
