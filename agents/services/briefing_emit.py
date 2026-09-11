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


# --- 01-08-2026: lapide anti-ressurreicao -----------------------------
import re as _re

_PREFIXOS_RESP = _re.compile(
    r"^(?:\s*(?:re|rv|res|rif|fw|fwd|enc|tr)\s*:\s*)+", _re.I)


def assunto_base(titulo: str) -> str:
    """Assunto sem prefixos de resposta, para comparar conversas."""
    base = _PREFIXOS_RESP.sub("", (titulo or "").strip())
    return _re.sub(r"\s+", " ", base).strip().lower()[:120]


# Tipos fora da familia "email" que tambem precisam de lapide. Sao os que
# descrevem um documento por tratar: se o David ja disse que esta tratado,
# nao volta, venha a chave de onde vier.
_TIPOS_COM_LAPIDE = (
    "fatura_sem_documento",
    "recibo_falta",
)


async def _fechado_equivalente(tipo: str, titulo: str, conta: str,
                               dias: int = 30):
    """Devolve o estado de um cartao equivalente ja fechado, ou None.

    A comparacao e feita em Python e nao em SQL de proposito: a
    normalizacao tem de ser exactamente a mesma que a do emissor, e duas
    implementacoes da mesma regex acabam sempre por divergir.
    """
    # 07-08-2026: os tipos de factura tambem precisam de lapide. Mudar o
    # esquema de chaves orfana o cartao antigo e cria um novo, e sem isto
    # uma decisao ja tomada volta a aparecer como se fosse trabalho novo.
    if tipo.startswith("email"):
        filtro_tipo = "tipo LIKE 'email%'"
    elif tipo in _TIPOS_COM_LAPIDE:
        filtro_tipo = "tipo = $3"
    else:
        return None
    alvo = assunto_base(titulo)
    if not alvo:
        return None
    from services.briefing_db import _get_pool
    pool = await _get_pool()
    async with pool.acquire() as conn:
        linhas = await conn.fetch(
            """
            SELECT titulo, status, resolvido_em
              FROM briefing_items
             WHERE status IN ('done', 'dismissed')
               AND """ + filtro_tipo + """
               AND resolvido_em > now() - ($1 || ' days')::interval
               AND ($2 = '' OR coalesce(metadata->>'account', '') = $2)
             ORDER BY resolvido_em DESC
             LIMIT 400
            """,
            str(dias), conta or "", tipo,
        )
    for l in linhas:
        if assunto_base(l["titulo"]) == alvo:
            return l["status"]
    return None


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

    # Se esta chave ainda nao existe, isto vai ser linha nova. Antes de a
    # criar, confirma-se que o David nao fechou ja este mesmo assunto.
    estado_lapide = None
    try:
        from services.briefing_db import _get_pool as _pool_lapide
        _p = await _pool_lapide()
        async with _p.acquire() as _c:
            _ja = await _c.fetchval(
                "SELECT status FROM briefing_items WHERE chave = $1", chave)
        if _ja is None:
            estado_lapide = await _fechado_equivalente(
                tipo, titulo, (metadata or {}).get("account") or "")
    except Exception as exc:
        log.warning("emit_briefing.lapide_falhou", tipo=tipo, err=str(exc))

    meta: dict[str, Any] = dict(metadata or {})
    meta["assunto_base"] = assunto_base(titulo)
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
        if item_id and estado_lapide:
            from services.briefing_db import _get_pool as _pool_fecho
            _pf = await _pool_fecho()
            async with _pf.acquire() as _cf:
                await _cf.execute(
                    "UPDATE briefing_items SET status = $2, resolvido_em = now() "
                    "WHERE id = $1::uuid AND status = 'open'",
                    item_id, estado_lapide)
            log.info("emit_briefing.ressurreicao_bloqueada", tipo=tipo,
                     titulo=titulo[:70], estado=estado_lapide)
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
