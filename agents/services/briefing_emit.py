"""briefing_emit: helper DRY para upsert em briefing_items.

Sprint 4.3 introduz este wrapper fino sobre briefing_db.upsert_item para
padronizar a emissao de cards a partir dos workers ricos. Sprint 5 vai
reusar o mesmo helper nos restantes 6 workers.

O wrapper trata de:
  * Construir o BriefingItem com defaults sensatos.
  * Gerar a chave estavel via make_chave(tipo, *chave_parts).
  * Preencher metadata['_emitter'] com worker_name e timestamp UTC.
  * Logar consistentemente sucesso/falha sem rebentar pipeline do worker.

Uso tipico::

    from services.briefing_emit import emit_briefing

    await emit_briefing(
        tipo="deal_stale",
        titulo="Deal X parado ha 18 dias",
        urgencia="P1",
        empresa="OMNAI",
        chave_parts=(deal_id,),
        link_origem=deal_url,
        metadata={"deal_id": deal_id},
        worker_name="pipeline-review-semanal",
    )
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import structlog

from services.briefing_db import BriefingItem, make_chave, upsert_item

log = structlog.get_logger()
_stdlog = logging.getLogger(__name__)


URGENCIAS_VALIDAS = ("P0", "P1", "P2", "P3")


async def emit_briefing(
    *,
    tipo: str,
    titulo: str,
    detalhe: str | None = None,
    urgencia: str = "P2",
    empresa: str,
    chave_parts: tuple[str, ...],
    link_origem: str | None = None,
    metadata: dict[str, Any] | None = None,
    worker_name: str = "",
) -> str | None:
    """Upsert idempotente de um card em briefing_items.

    Args:
        tipo: categoria estavel (e.g. "deal_stale", "concurso_novo").
        titulo: linha curta visivel no inbox executivo.
        detalhe: texto descritivo opcional, mostrado no callout.
        urgencia: P0/P1/P2/P3. Default P2.
        empresa: OMNAI/Previnsa/JMSoares/Sopato/Pessoal.
        chave_parts: partes que compoem a chave estavel; passadas a
            make_chave(tipo, *chave_parts). Re-execucoes com as mesmas
            partes nao duplicam.
        link_origem: URL para o item original (ex: pagina Notion).
        metadata: dict serializavel JSON. O helper acrescenta a chave
            "_emitter" com worker_name e timestamp.
        worker_name: nome do worker emissor (logs + metadata).

    Returns:
        id da linha em briefing_items, ou None em caso de falha.
    """
    if urgencia not in URGENCIAS_VALIDAS:
        log.warning("emit_briefing.urgencia_invalida", urgencia=urgencia, worker=worker_name)
        urgencia = "P2"

    if not chave_parts:
        log.warning("emit_briefing.chave_parts_vazias", tipo=tipo, worker=worker_name)
        return None

    chave = make_chave(tipo, *(str(p) for p in chave_parts))

    meta: dict[str, Any] = dict(metadata or {})
    meta["_emitter"] = {
        "worker": worker_name or "unknown",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    item = BriefingItem(
        chave=chave,
        tipo=tipo,
        titulo=titulo[:500],
        urgencia=urgencia,
        empresa=empresa,
        detalhe=detalhe,
        link_origem=link_origem,
        metadata=meta,
    )

    try:
        item_id = await upsert_item(item)
        log.info(
            "emit_briefing.ok",
            worker=worker_name,
            tipo=tipo,
            urgencia=urgencia,
            empresa=empresa,
            chave=chave[:12],
            item_id=item_id,
        )
        return item_id
    except Exception as exc:
        log.error(
            "emit_briefing.fail",
            worker=worker_name,
            tipo=tipo,
            empresa=empresa,
            chave=chave[:12],
            err=str(exc),
        )
        _stdlog.exception("emit_briefing failed for %s/%s", worker_name, tipo)
        return None


def page_url_from_id(page_id: str | None) -> str | None:
    """Constroi URL canonica Notion a partir de um page_id.

    NotionClient.create_database_row e create_child_page retornam o page_id
    (uuid com hifens). O URL publico no Notion usa o id sem hifens.
    """
    if not page_id:
        return None
    return f"https://www.notion.so/{page_id.replace('-', '')}"
