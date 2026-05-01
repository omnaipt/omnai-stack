"""Worker: verificacao-recibos-sopato | Ana | CFO

Sprint 4.2: reescrito a partir das interfaces reais do VPS.

Cron: dia 5 de cada mes as 10:00 UTC.

Mudancas face a versao Sprint 4 (com bugs):
    - Em vez de inventar `notion.financeiro.find_recibos`, usa a logica real
      ja em producao: scan ao filesystem /faturas/Sopato/YYYY-Qx para detectar
      PDFs do mes anterior, com matching por slugify(nome_inquilino).
      Ver `omnai-agents-current/workers/verificacao_recibos_sopato.py:55-65`.
    - Schema briefing_items real (chave, tipo, urgencia, empresa, titulo,
      detalhe, link_origem, metadata) em vez do schema fictício
      (worker, dedupe_key, title, body, severity).
      Ver `services/briefing_db.py:38-48` e `:61-100`.
    - Acesso a Postgres via `services.briefing_db.upsert_item`, alinhado com
      `workers/briefing_inbox.py:17` (sem `from agents.services.db import get_pool`).
    - Skip de inquilinos em INQUILINOS_SEM_FATURA (Ricardo Rocha) usando o
      novo helper `services.sopato_config`.
    - Pre-aquece a cache de inquilinos com `load_inquilinos_from_notion()`
      antes de chamar `listar_inquilinos_activos()`.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

import structlog

from services import briefing_db, notion_ext
from services.briefing_db import BriefingItem, make_chave
from services.notion_ext import paragraph
from services.sopato_config import (
    INQUILINOS_SEM_FATURA,
    Inquilino,
    listar_inquilinos_activos,
    load_inquilinos_from_notion,
)
from services.text_utils import slugify

log = structlog.get_logger()


ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"

FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
SOPATO_ROOT = FATURAS_ROOT / "Sopato"


MONTH_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Marco", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _mes_anterior(today: date | None = None) -> tuple[int, int]:
    today = today or date.today()
    primeiro = date(today.year, today.month, 1)
    ultimo = primeiro - timedelta(days=1)
    return ultimo.year, ultimo.month


def _quarter(month: int) -> int:
    return (month - 1) // 3 + 1


def _pasta_mes(ano: int, mes: int) -> Path:
    return SOPATO_ROOT / f"{ano}-Q{_quarter(mes)}"


def _pdfs_do_mes(ano: int, mes: int) -> list[Path]:
    pasta = _pasta_mes(ano, mes)
    if not pasta.exists():
        return []
    return [p for p in pasta.iterdir() if p.suffix.lower() == ".pdf"]


def _matches(pdf_name: str, inq: Inquilino) -> bool:
    """Detecta correspondencia entre nome do PDF e o inquilino.

    Match por slug do nome ou por notion_id (quando disponivel).
    Mantem a heuristica do worker actual (linha 62-65 do ficheiro VPS).
    """
    slug_inq = slugify(inq.nome)
    slug_pdf = slugify(pdf_name)
    if slug_inq and slug_inq in slug_pdf:
        return True
    if inq.notion_id and inq.notion_id.lower() in pdf_name.lower():
        return True
    return False


async def run() -> dict:
    ano, mes = _mes_anterior()
    mes_nome = MONTH_PT[mes]

    # Pre-aquece a cache a partir do Notion (com fallback para seed JSON).
    try:
        await load_inquilinos_from_notion()
    except Exception as exc:  # noqa: BLE001
        log.warning("load_inquilinos_from_notion FAIL", err=str(exc))
    inquilinos = listar_inquilinos_activos()

    pdfs = _pdfs_do_mes(ano, mes)

    em_falta: list[Inquilino] = []
    encontrados: list[tuple[Inquilino, Path]] = []
    skipped_sem_fatura: list[Inquilino] = []

    for inq in inquilinos:
        # Skip explicito: pagamento em dinheiro / sem fatura por opcao do David.
        if inq.sem_fatura or inq.nome in INQUILINOS_SEM_FATURA:
            skipped_sem_fatura.append(inq)
            log.info("skip sem_fatura", inquilino=inq.nome)
            continue

        match = next((p for p in pdfs if _matches(p.name, inq)), None)
        if match is None:
            em_falta.append(inq)
        else:
            encontrados.append((inq, match))

    # Upsert em briefing_items: 1 card por inquilino em falta.
    cards_criados = 0
    for inq in em_falta:
        chave = make_chave("sopato_recibo", inq.id, f"{ano}-{mes:02d}")
        notas_extra = (inq.notas or "").strip()
        detalhe_lines = [f"Mes: {mes_nome} {ano}"]
        if inq.fraccao:
            detalhe_lines.append(f"Fraccao: {inq.fraccao}")
        if inq.renda_mensal:
            detalhe_lines.append(f"Renda: {inq.renda_mensal:.2f} EUR")
        if notas_extra:
            detalhe_lines.append(notas_extra)

        # Anti-incidente: nomes historicamente problematicos sobem para P0.
        nome_lower = inq.nome.lower()
        urgencia = "P0" if "joao candido" in nome_lower or "candido" in nome_lower else "P1"

        await briefing_db.upsert_item(BriefingItem(
            chave=chave,
            tipo="recibo_falta",
            urgencia=urgencia,
            empresa="Sopato",
            titulo=f"Emitir recibo {inq.nome} | Fraccao {inq.fraccao or 's/fraccao'}",
            detalhe=" | ".join(detalhe_lines),
            link_origem=None,
            metadata={
                "inquilino_id": inq.id,
                "inquilino_nome": inq.nome,
                "ano": ano,
                "mes": mes,
                "fraccao": inq.fraccao,
                "notion_id": inq.notion_id,
            },
        ))
        cards_criados += 1

    # Activity Log no Notion (audit trail, nao para o briefing inbox).
    log_title = (
        f"Sopato {'ALERTA' if em_falta else 'OK'} | Recibos {mes_nome} {ano} | "
        f"{len(encontrados)}/{len(inquilinos) - len(skipped_sem_fatura)} encontrados"
    )
    if skipped_sem_fatura:
        log_title += f" | {len(skipped_sem_fatura)} skip sem-fatura"

    detalhes = [
        f"{cards_criados} card(s) criado(s) no inbox briefing." if em_falta
        else "Verificacao automatica passou. Sem cards a criar.",
    ]
    if skipped_sem_fatura:
        nomes = ", ".join(i.nome for i in skipped_sem_fatura)
        detalhes.append(f"Skip sem fatura: {nomes}.")

    try:
        await notion_ext.create_database_row(
            data_source_id=ACTIVITY_LOG_DB,
            title=log_title,
            children=[paragraph(line) for line in detalhes],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("activity_log create FAIL", err=str(exc))

    out = {
        "status": "ok",
        "mes": f"{mes_nome} {ano}",
        "inquilinos_total": len(inquilinos),
        "skipped_sem_fatura": len(skipped_sem_fatura),
        "em_falta_count": len(em_falta),
        "encontrados_count": len(encontrados),
        "cards_criados": cards_criados,
    }
    log.info("verificacao-recibos-sopato", **out)
    return out
