"""
notion_inquilinos.py

Helper isolado para ler e fazer parsing da database Notion "Inquilinos Sopato".
Separa concerns de sopato_config: este modulo so sabe falar com Notion e
devolver dicts limpos. A camada de cache, fallback e dataclass vive em
sopato_config.

Database Notion:
    Collection ID: 6e8290fd-b8a8-4232-a0ff-50bc2961a620
    Schema:
        Nome (title)
        Fraccao (rich_text)
        Email (email)
        Telefone (phone_number)
        Renda Mensal (number, format euro)
        Inicio Contrato (date)
        Fim Contrato (date)
        Status (select: Activo, Inactivo, Em saida)
        Notas (rich_text)

CORRECAO Sprint 4.2: NotionClient real (services/notion.py:42) expoe
`query_data_source(data_source_id, filter_, sorts, page_size)`. Nao existe
`client.data_sources.query()`. Substituido para usar a interface real.
Paginacao manual nao e suportada pelo cliente actual (page_size max 100),
o que cobre o caso Sopato (5 inquilinos).
"""

from __future__ import annotations

import logging
from typing import Any

from services.notion import NotionClient

log = logging.getLogger(__name__)

# NotionVersion 2022-06-28 (em uso no VPS) bate em /v1/databases/{id}/query e
# espera database_id legacy, nao data_source_id de versoes >=2025-09-03.
# Database "Inquilinos Sopato" criada 01-05-2026 com:
#   database_id    = eb85be7f-9a88-468c-b0c8-a52d9b4c86be  <- ESTE para 2022-06-28
#   data_source_id = 6e8290fd-b8a8-4232-a0ff-50bc2961a620  (so >= 2025-09-03)
# Validado via curl directo: query a database_id devolve 5 inquilinos OK.
INQUILINOS_DATA_SOURCE_ID = "eb85be7f-9a88-468c-b0c8-a52d9b4c86be"


def _extract_title(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    items = prop.get("title") or []
    if not items:
        return None
    return "".join(part.get("plain_text", "") for part in items).strip() or None


def _extract_rich_text(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    items = prop.get("rich_text") or []
    if not items:
        return None
    return "".join(part.get("plain_text", "") for part in items).strip() or None


def _extract_email(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    value = prop.get("email")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _extract_phone(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    value = prop.get("phone_number")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _extract_number(prop: dict[str, Any] | None) -> float | None:
    if not prop:
        return None
    value = prop.get("number")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_date(prop: dict[str, Any] | None) -> str | None:
    """Devolve a data ISO start, ou None. Notion guarda como {start, end, time_zone}."""
    if not prop:
        return None
    date = prop.get("date") or {}
    start = date.get("start")
    return start if isinstance(start, str) and start else None


def _extract_select(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    select = prop.get("select") or {}
    name = select.get("name")
    return name if isinstance(name, str) and name else None


def parse_inquilino_page(page: dict[str, Any]) -> dict[str, Any]:
    """
    Converte uma page Notion num dict normalizado.
    Nao levanta excepcoes para campos vazios: devolve None nesses campos
    e deixa a validacao para o caller.
    """
    props = page.get("properties") or {}

    return {
        "notion_id": page.get("id"),
        "nome": _extract_title(props.get("Nome")),
        "fraccao": _extract_rich_text(props.get("Fraccao")),
        "email": _extract_email(props.get("Email")),
        "telefone": _extract_phone(props.get("Telefone")),
        "renda_mensal": _extract_number(props.get("Renda Mensal")),
        "inicio_contrato": _extract_date(props.get("Inicio Contrato")),
        "fim_contrato": _extract_date(props.get("Fim Contrato")),
        "status": _extract_select(props.get("Status")),
        "notas": _extract_rich_text(props.get("Notas")),
    }


async def fetch_inquilinos_raw(
    client: NotionClient,
    *,
    only_active: bool = True,
) -> list[dict[str, Any]]:
    """
    Faz query a database Notion e devolve lista de dicts normalizados.
    Nao aplica cache nem fallback: e responsabilidade do caller.

    Args:
        client: instancia ja autenticada de NotionClient (services.notion).
        only_active: se True, filtra Status != Inactivo no servidor Notion.
    """
    notion_filter: dict[str, Any] | None = None
    if only_active:
        notion_filter = {
            "property": "Status",
            "select": {"does_not_equal": "Inactivo"},
        }

    response = await client.query_data_source(
        data_source_id=INQUILINOS_DATA_SOURCE_ID,
        filter_=notion_filter,
        page_size=100,
    )

    results: list[dict[str, Any]] = []
    for page in response.get("results", []) or []:
        parsed = parse_inquilino_page(page)
        if not parsed.get("nome"):
            log.warning(
                "inquilino sem nome ignorado, page_id=%s", parsed.get("notion_id")
            )
            continue
        results.append(parsed)

    if response.get("has_more"):
        log.warning(
            "fetch_inquilinos_raw: has_more=True mas o NotionClient actual "
            "nao suporta start_cursor; resultados truncados a %d",
            len(results),
        )

    log.info("fetch_inquilinos_raw devolveu %d inquilinos", len(results))
    return results
