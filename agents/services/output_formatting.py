"""Output formatting helpers para workers n8n.

Centraliza o formato dos outputs dos workers para cumprir as 5 melhorias
transversais (Fase 2 OMNAI Agents):

1. Scorecard obrigatorio no topo de cada pagina (3 linhas).
2. Suprimir paginas vazias - so regista em Activity Log.
3. Back-references linkadas (helpers de URL/block).
4. Accoes executaveis inline (to-do blocks identificaveis pelo Carlos).
5. Pagina-mestra diaria "Hoje - DD/MM" (feita por worker separado).

Consumo tipico:

    from services.output_formatting import (
        Scorecard, ScorecardRow, should_publish, activity_log_minimal,
        scorecard_blocks, action_todo, back_ref_bullet,
    )

    sc = Scorecard(
        atencao=[ScorecardRow("3 concursos com prazo esta semana")],
        automatizado=[ScorecardRow("12 novos classificados")],
        falhas=[],
    )
    if not should_publish(sc):
        await activity_log_minimal("scan-concursos", "0 novos, 0 activos")
        return {"status": "ok", "skipped_empty": True}

    blocks = scorecard_blocks(sc) + [action_todo("Revisar concurso 5547/2026"), ...]
    await notion_ext.create_child_page(parent_page_id=..., title=..., children=blocks)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from services import notion_ext

ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"


@dataclass
class ScorecardRow:
    """Uma linha do scorecard. O `link` e opcional (para back-ref)."""
    text: str
    link: str | None = None


@dataclass
class Scorecard:
    """3 baldes do scorecard de topo. Podem ter 0 ou mais linhas cada."""
    atencao: list[ScorecardRow] = field(default_factory=list)
    automatizado: list[ScorecardRow] = field(default_factory=list)
    falhas: list[ScorecardRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.atencao) + len(self.automatizado) + len(self.falhas)

    @property
    def has_signal(self) -> bool:
        """True se vale a pena criar pagina (tem atencao ou falhas)."""
        return bool(self.atencao) or bool(self.falhas)


def should_publish(sc: Scorecard) -> bool:
    """#2 suprimir-quando-vazio: so cria pagina se ha algo que o David deva ver.

    Se so ha 'automatizado' (rotina normal sem novidade), basta Activity Log.
    Se ha 'atencao' ou 'falhas', cria pagina.
    """
    return sc.has_signal


def scorecard_blocks(sc: Scorecard) -> list[dict[str, Any]]:
    """#1 gera os 3 blocos de topo obrigatorios para qualquer pagina worker.

    Output: heading "Scorecard" + 3 callouts (atencao / automatizado / falhas).
    """
    def _callout(icon: str, titulo: str, rows: list[ScorecardRow]) -> dict[str, Any]:
        txt = titulo if not rows else f"{titulo} ({len(rows)})"
        block = {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": txt}}],
                "icon": {"type": "emoji", "emoji": icon},
            },
        }
        if rows:
            block["callout"]["children"] = [
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": (
                                    {"content": r.text, "link": {"url": r.link}}
                                    if r.link else {"content": r.text}
                                ),
                            }
                        ],
                    },
                }
                for r in rows
            ]
        return block

    return [
        notion_ext.heading(2, "Scorecard"),
        _callout("\u26a0\ufe0f", "Atencao - requer accao tua", sc.atencao),
        _callout("\u2705", "Automatizado - sem accao", sc.automatizado),
        _callout("\u274c", "Falhas tecnicas", sc.falhas),
        {"object": "block", "type": "divider", "divider": {}},
    ]


def action_todo(text: str, link: str | None = None) -> dict[str, Any]:
    """#4 accao executavel inline - bloco to-do que o David / Carlos vao acompanhar.

    Quando checked pelo David, workers de monitoring podem reagir (V2).
    """
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [
                {
                    "type": "text",
                    "text": (
                        {"content": text, "link": {"url": link}}
                        if link else {"content": text}
                    ),
                }
            ],
            "checked": False,
        },
    }


def back_ref_bullet(text: str, link: str) -> dict[str, Any]:
    """#3 bullet com link directo a entidade Notion (deal, fatura, inquilino, etc).

    `link` deve ser a URL completa da pagina (https://www.notion.so/...).
    """
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [
                {"type": "text", "text": {"content": text, "link": {"url": link}}}
            ],
        },
    }


async def activity_log_minimal(source: str, resumo: str) -> None:
    """#2 registo curto na Activity Log quando suprimimos pagina por vazia.

    Mantem rasto de execucao sem criar ruido visual. O David pode filtrar a
    Activity Log por 'Processado=false' para ver so o que precisa atencao.
    """
    await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=f"{source} | {date.today().isoformat()} | {resumo}",
    )
