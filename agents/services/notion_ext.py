"""Helpers de alto nivel em cima de services.notion.NotionClient.

v9.1.1: simplifica. IDs tratados como database_ids (legacy) por defeito;
fallback para data_source_id se a API 2025-09-03 rejeitar. Elimina o
GET /data_sources que dava 400 em IDs legacy.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from services.notion import NotionClient

logger = logging.getLogger(__name__)

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")
BASE = "https://api.notion.com/v1"


def _headers() -> dict[str, str]:
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN nao definido")
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def rt(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def heading(level: int, text: str) -> dict[str, Any]:
    key = f"heading_{max(1, min(3, level))}"
    return {"object": "block", "type": key, key: {"rich_text": rt(text)}}


def paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(text)}}


def bullet(text: str, link: str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {"content": text[:2000]}
    if link:
        content["link"] = {"url": link}
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": content}]},
    }


async def _discover_title_prop(client: httpx.AsyncClient, db_id: str) -> str:
    for endpoint in (f"{BASE}/databases/{db_id}", f"{BASE}/data_sources/{db_id}"):
        try:
            r = await client.get(endpoint)
            if r.status_code == 200:
                props_def = r.json().get("properties", {})
                for name, p in props_def.items():
                    if p.get("type") == "title":
                        return name
        except Exception:
            continue
    return "Name"


async def create_database_row(
    *,
    data_source_id: str,
    title: str,
    children: list[dict[str, Any]] | None = None,
    properties_extra: dict[str, Any] | None = None,
    title_prop_name: str | None = None,
) -> str | None:
    async with httpx.AsyncClient(headers=_headers(), timeout=30.0) as client:
        if title_prop_name is None:
            title_prop_name = await _discover_title_prop(client, data_source_id)

        props: dict[str, Any] = {title_prop_name: {"title": rt(title)}}
        if properties_extra:
            props.update(properties_extra)

        body_db: dict[str, Any] = {
            "parent": {"database_id": data_source_id},
            "properties": props,
        }
        if children:
            body_db["children"] = children[:100]

        try:
            r = await client.post(f"{BASE}/pages", json=body_db)
            if r.status_code < 400:
                page_id = r.json().get("id")
                if page_id and children and len(children) > 100:
                    async with NotionClient() as nc:
                        await nc.append_blocks(page_id, children[100:])
                return page_id
            logger.info(
                "create_database_row db parent %s failed %s, trying data_source",
                data_source_id, r.status_code,
            )
        except Exception as exc:
            logger.warning("create_database_row db parent exc=%s", exc)

        body_ds: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": props,
        }
        if children:
            body_ds["children"] = children[:100]

        try:
            r = await client.post(f"{BASE}/pages", json=body_ds)
            if r.status_code < 400:
                page_id = r.json().get("id")
                if page_id and children and len(children) > 100:
                    async with NotionClient() as nc:
                        await nc.append_blocks(page_id, children[100:])
                return page_id
            logger.warning(
                "create_database_row FAIL db=%s status=%s body=%s",
                data_source_id, r.status_code, r.text[:300],
            )
        except Exception as exc:
            logger.error("create_database_row data_source exc=%s", exc)

        return None


async def create_child_page(
    *,
    parent_page_id: str,
    title: str,
    children: list[dict[str, Any]],
) -> str | None:
    async with httpx.AsyncClient(headers=_headers(), timeout=30.0) as client:
        body = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": rt(title)}},
            "children": children[:100],
        }
        try:
            r = await client.post(f"{BASE}/pages", json=body)
            if r.status_code >= 400:
                logger.warning("create_child_page FAIL status=%s body=%s", r.status_code, r.text[:300])
                return None

            page_id = r.json().get("id")
            if page_id and len(children) > 100:
                async with NotionClient() as nc:
                    await nc.append_blocks(page_id, children[100:])
            return page_id
        except Exception as exc:
            logger.error("create_child_page exc=%s", exc)
            return None


async def append_children(page_id: str, blocks: list[dict[str, Any]]) -> None:
    async with NotionClient() as nc:
        await nc.append_blocks(page_id, blocks)


async def query_database(
    *, data_source_id: str, filter_: dict | None = None, page_size: int = 100
) -> list[dict]:
    async with NotionClient() as nc:
        resp = await nc.query_data_source(
            data_source_id=data_source_id,
            filter_=filter_,
            page_size=page_size,
        )
        return resp.get("results", [])
