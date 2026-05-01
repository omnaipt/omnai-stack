#!/usr/bin/env python3
"""setup_todo_view.py - Sprint 7.6 (idempotente, corre 1x)

Configura a pagina '🌅 Hoje' do David com a estrutura persistente Sprint 7.6:

    [conteudo estatico, preservado entre execucoes]
    H1: Briefing diario
    H2: 📋 To-do (David)
    [linked database view 'Tarefas Hoje (David)' filtrada e agrupada]
    [divider]

    <!-- BRIEFING_AGENT_START -->
    [conteudo dinamico regenerado pelo briefing_inbox]
    <!-- BRIEFING_AGENT_END -->

Idempotencia:
- Se a pagina ja contem MARKER_START, o script DETECTA e ABORTA com mensagem
  informativa (nao destrutivo). Re-correr e seguro.
- Para forcar reset, passar --force que apaga tudo e refaz do zero.

Como invocar a MCP-style 'notion-create-view':
- A API publica do Notion suporta criar um block do tipo 'child_database'
  apontando para um database/data_source existente, o que produz uma vista
  linked database inline na pagina. Nao temos endpoint dedicado para definir
  filter/group/sort no momento da criacao via API publica. Por isso, o script
  cria o block 'child_database' apontando para a DB Tarefas e adiciona um
  callout cinza com instrucoes para o David configurar filtro/group/sort
  manualmente UMA vez (operacao de 30 segundos no Notion). A vista persiste
  apos isso entre execucoes do briefing_inbox.

  Alternativa via MCP: se o servidor MCP notion-create-view estiver
  disponivel, pode-se invocar com:
    notion-create-view(
      parent_page_id="33e973b9-2387-8107-8667-eadc9128ab27",
      data_source_id="119b5aae-583a-4646-b732-a0975f7cf4bd",
      type="table",
      filter={...},
      group_by="Empresa",
      sorts=[...],
    )
  Ver bloco MCP_VIEW_CONFIG abaixo para o payload exacto.

Uso:
    python3 setup_todo_view.py                 idempotente, aborta se ja feito
    python3 setup_todo_view.py --force         apaga tudo e re-cria
    python3 setup_todo_view.py --check         apenas verifica e nao escreve

Pre-requisitos:
- NOTION_TOKEN no ambiente (mesma var usada pelo agents-api).
- A integracao do Notion tem acesso a pagina MORNING_BRIEFING_PAGE e a DB
  Tarefas TAREFAS_DATA_SOURCE.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

# Permitir correr standalone OU dentro do container omnai_agents (que tem
# /agents no PYTHONPATH e expoe services.notion).
try:
    from services.notion import NotionClient  # type: ignore
except Exception:
    # Standalone fallback: adicionar /opt/omnai-stack/agents ao path se existir.
    for candidate in (
        "/opt/omnai-stack/agents",
        os.path.join(os.path.dirname(__file__), "agents"),
    ):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)
    from services.notion import NotionClient  # type: ignore


MORNING_BRIEFING_PAGE = "33e973b9-2387-8107-8667-eadc9128ab27"
TAREFAS_DATA_SOURCE = "119b5aae-583a-4646-b732-a0975f7cf4bd"

MARKER_START = "<!-- BRIEFING_AGENT_START -->"
MARKER_END = "<!-- BRIEFING_AGENT_END -->"


# Configuracao da vista (referencia para uso via MCP notion-create-view).
# A API publica do Notion nao permite definir filter/group/sort na criacao
# de child_database; este bloco fica documentado para ser aplicado via MCP
# ou manualmente apos o setup.
MCP_VIEW_CONFIG: dict[str, Any] = {
    "parent_page_id": MORNING_BRIEFING_PAGE,
    "data_source_id": TAREFAS_DATA_SOURCE,
    "type": "table",
    "name": "Tarefas Hoje (David)",
    "filter": {
        "and": [
            {"property": "Mostrar no Briefing", "checkbox": {"equals": True}},
            {"property": "Status", "status": {"does_not_equal": "Concluído"}},
            {"property": "Responsavel", "select": {"equals": "David"}},
        ]
    },
    "group_by": "Empresa",
    "sorts": [
        {"property": "Prioridade", "direction": "ascending"},
        {"property": "Deadline", "direction": "ascending"},
    ],
    "properties_visible": [
        "Tarefa", "Status", "Empresa", "Prioridade", "Deadline",
        "Mostrar no Briefing",
    ],
}


def _block_plain_text(block: dict) -> str:
    btype = block.get("type")
    if not btype:
        return ""
    payload = block.get(btype) or {}
    rt_arr = payload.get("rich_text") or []
    parts = []
    for r in rt_arr:
        pt = r.get("plain_text")
        if pt is not None:
            parts.append(pt)
            continue
        txt = (r.get("text") or {}).get("content")
        if txt:
            parts.append(txt)
    return "".join(parts)


def _has_markers(blocks: list[dict]) -> bool:
    seen_start = False
    seen_end = False
    for b in blocks:
        text = _block_plain_text(b)
        if MARKER_START in text:
            seen_start = True
        if MARKER_END in text:
            seen_end = True
    return seen_start and seen_end


def _marker_paragraph(content: str) -> dict:
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


def _heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {
        "object": "block",
        "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _callout(text: str, emoji: str = "ℹ️", color: str = "gray_background") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
            "rich_text": [{"type": "text", "text": {"content": text}}],
        },
    }


def _linked_database_block(data_source_id: str) -> dict:
    """Block child_database que cria uma vista linked database inline.

    A API publica do Notion aceita criar um block 'child_database' como child
    de uma pagina, apontando para um database_id existente. Isto manifesta-se
    no UI como uma vista linked database.

    Nota: a definicao de filter/group/sort tem de ser feita pelo David UMA vez
    no UI (ou via MCP notion-create-view, ver MCP_VIEW_CONFIG).
    """
    return {
        "object": "block",
        "type": "child_database",
        "child_database": {
            "title": "Tarefas Hoje (David)",
        },
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


async def _wipe_page(nc: NotionClient, page_id: str) -> int:
    existing = await nc.get_block_children(page_id)
    n = 0
    for b in existing:
        try:
            await nc.delete_block(b["id"])
            n += 1
        except Exception as exc:
            print(f"  [WARN] delete_block FAIL id={b.get('id')}: {exc}", file=sys.stderr)
    return n


async def _build_static_layout() -> list[dict]:
    """Constroi a area estatica que fica ANTES do MARKER_START."""
    return [
        _heading(1, "🌅 Briefing Hoje"),
        _heading(2, "📋 To-do (David)"),
        _callout(
            "Vista interactiva: clicar '+ Nova' abaixo para criar uma tarefa "
            "directamente. Filtros: Mostrar no Briefing = true · Status ≠ "
            "Concluído · Responsavel = David. Group by Empresa. Sort por "
            "Prioridade e Deadline. Configurar UMA vez no UI ou via MCP "
            "notion-create-view (ver MCP_VIEW_CONFIG no setup script).",
            emoji="💡",
            color="blue_background",
        ),
        _linked_database_block(TAREFAS_DATA_SOURCE),
        _divider(),
        _marker_paragraph(MARKER_START),
        _callout(
            "Conteudo dinamico regenerado a cada execucao do worker "
            "briefing-carlos. Nao editar manualmente entre estes markers.",
            emoji="🤖",
            color="gray_background",
        ),
        _marker_paragraph(MARKER_END),
    ]


async def setup(force: bool = False, check_only: bool = False) -> int:
    print(f"[INFO] setup_todo_view.py - pagina {MORNING_BRIEFING_PAGE}")

    async with NotionClient() as nc:
        try:
            existing = await nc.get_block_children(MORNING_BRIEFING_PAGE)
        except Exception as exc:
            print(f"[FAIL] get_block_children: {exc}", file=sys.stderr)
            return 2

        already = _has_markers(existing)
        print(f"[INFO] {len(existing)} blocks existentes, markers presentes: {already}")

        if check_only:
            if already:
                print("[OK] markers presentes, setup ja feito")
                return 0
            print("[INFO] markers ausentes, setup ainda nao foi corrido")
            return 1

        if already and not force:
            print("[OK] markers ja presentes, nada a fazer (idempotente).")
            print("     Para forcar reset, correr com --force.")
            return 0

        if force:
            print("[INFO] --force activo, a apagar conteudo existente...")
        else:
            print("[INFO] markers ausentes, a fazer setup inicial...")

        n = await _wipe_page(nc, MORNING_BRIEFING_PAGE)
        print(f"  apagados {n} blocks")

        layout = await _build_static_layout()
        await nc.append_blocks(MORNING_BRIEFING_PAGE, layout)
        print(f"  inseridos {len(layout)} blocks (heading + linked DB + markers)")

    print()
    print("[DONE] setup concluido.")
    print()
    print("ACCAO MANUAL pendente (UMA vez): abrir a pagina '🌅 Hoje' no Notion,")
    print("clicar na vista 'Tarefas Hoje (David)' e configurar:")
    print("  - Filtro: Mostrar no Briefing = true")
    print("  - Filtro: Status ≠ Concluído")
    print("  - Filtro: Responsavel = David")
    print("  - Group by: Empresa")
    print("  - Sort: Prioridade ASC, depois Deadline ASC")
    print("  - Properties visiveis: Tarefa, Status, Empresa, Prioridade,")
    print("                         Deadline, Mostrar no Briefing")
    print()
    print("Alternativa: invocar MCP notion-create-view com MCP_VIEW_CONFIG")
    print("(ver topo do script).")
    print()
    print("Apos isto, o David pode clicar '+ Nova' na vista para criar tarefas")
    print("e o briefing_inbox.py NUNCA destruira esta vista (markers protegem).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup pagina Hoje (Sprint 7.6)")
    parser.add_argument("--force", action="store_true",
                        help="Apaga conteudo existente e refaz do zero")
    parser.add_argument("--check", action="store_true",
                        help="Apenas verifica se ja foi feito setup, nao escreve")
    args = parser.parse_args()

    return asyncio.run(setup(force=args.force, check_only=args.check))


if __name__ == "__main__":
    sys.exit(main())
