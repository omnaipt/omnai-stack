"""Helpers para construir blocos Notion."""
from __future__ import annotations


def _rt_obj(text: str) -> dict:
    return {"type": "text", "text": {"content": text[:2000]}}


def rt(text: str) -> dict:
    """Um objecto rich_text (para incluir em listas [rt(x)])."""
    return _rt_obj(text)


def rt_array(text: str) -> list[dict]:
    """Array rich_text pronto (para celulas de tabela)."""
    return [_rt_obj(text)]


def rt_array_with_link(text: str, url: str) -> list[dict]:
    return [{
        "type": "text",
        "text": {"content": text[:2000], "link": {"url": url}},
    }]


def heading_1(text: str) -> dict:
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": [rt(text)]}}


def heading_2(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rt(text)]}}


def heading_3(text: str) -> dict:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [rt(text)]}}


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [rt(text)]}}


def paragraph_link(text: str, url: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rt_array_with_link(text, url)},
    }


def bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [rt(text)]},
    }


def to_do(text: str, checked: bool = False) -> dict:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {"rich_text": [rt(text)], "checked": checked},
    }


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def callout(text: str, emoji: str = "💡") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [rt(text)],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def bookmark(url: str, caption: str = "") -> dict:
    body: dict = {"url": url}
    if caption:
        body["caption"] = [rt(caption)]
    return {"object": "block", "type": "bookmark", "bookmark": body}


def table(rows: list[list[list[dict]]], has_column_header: bool = True) -> dict:
    """Cria um bloco table.

    rows = lista de linhas; cada linha e lista de celulas; cada celula e um
    rich_text array (produzido por rt_array() ou rt_array_with_link()).
    """
    width = len(rows[0]) if rows else 0
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": has_column_header,
            "has_row_header": False,
            "children": [
                {"type": "table_row", "table_row": {"cells": r}} for r in rows
            ],
        },
    }


def markdown_to_blocks(md: str) -> list[dict]:
    """Converte markdown simples em blocos Notion.

    Suporta: headings #, ##, ###; bullets - e *; to-do [ ] e [x]; paragrafos.
    """
    blocks: list[dict] = []
    for raw in md.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("### "):
            blocks.append(heading_3(line[4:].strip()))
        elif line.startswith("## "):
            blocks.append(heading_2(line[3:].strip()))
        elif line.startswith("# "):
            blocks.append(heading_1(line[2:].strip()))
        elif stripped.startswith(("- [ ] ", "* [ ] ")):
            blocks.append(to_do(stripped[6:].strip(), checked=False))
        elif stripped.startswith(("- [x] ", "* [x] ", "- [X] ", "* [X] ")):
            blocks.append(to_do(stripped[6:].strip(), checked=True))
        elif stripped.startswith(("- ", "* ")):
            blocks.append(bullet(stripped[2:].strip()))
        else:
            blocks.append(paragraph(line))
    return blocks
