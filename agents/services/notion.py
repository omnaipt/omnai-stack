"""Cliente minimo da API do Notion.

Usa o endpoint data_sources (Notion API 2025-09-03) com fallback para databases
caso o tenant ainda esteja na API antiga.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")
BASE = "https://api.notion.com/v1"


class NotionClient:
    def __init__(self) -> None:
        if not NOTION_TOKEN:
            raise RuntimeError("NOTION_TOKEN nao definido")
        self.headers = {
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self.headers, timeout=30.0)
        return self._client

    async def __aenter__(self) -> "NotionClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def query_data_source(
        self,
        data_source_id: str,
        filter_: dict | None = None,
        sorts: list | None = None,
        page_size: int = 50,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": page_size}
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        c = self._client_or_new()
        # Tentar data_sources primeiro (API nova)
        # API publica Notion: so /databases/{id}/query existe oficialmente.
        # /data_sources/{id}/query nao e endpoint valido (400 invalid_request_url).
        r = await c.post(f"{BASE}/databases/{data_source_id}/query", json=body)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Notion query {data_source_id} falhou: {r.status_code} {r.text[:300]}",
                request=r.request,
                response=r,
            )
        return r.json()

    async def get_page(self, page_id: str) -> dict[str, Any]:
        r = await self._client_or_new().get(f"{BASE}/pages/{page_id}")
        r.raise_for_status()
        return r.json()

    async def get_block_children(self, block_id: str) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        c = self._client_or_new()
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = await c.get(f"{BASE}/blocks/{block_id}/children", params=params)
            r.raise_for_status()
            data = r.json()
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    async def delete_block(self, block_id: str) -> None:
        r = await self._client_or_new().delete(f"{BASE}/blocks/{block_id}")
        # Ignorar 404 (bloco ja apagado) e archived
        if r.status_code not in (200, 404):
            r.raise_for_status()

    async def append_blocks(self, block_id: str, blocks: list[dict]) -> dict:
        # Notion limita 100 blocos por append
        out: dict[str, Any] = {}
        c = self._client_or_new()
        for i in range(0, len(blocks), 100):
            chunk = blocks[i : i + 100]
            r = await c.patch(
                f"{BASE}/blocks/{block_id}/children",
                json={"children": chunk},
            )
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"Notion append falhou: {r.status_code} {r.text[:300]}",
                    request=r.request,
                    response=r,
                )
            out = r.json()
        return out

    async def replace_page_content(
        self, page_id: str, blocks: list[dict]
    ) -> dict:
        existing = await self.get_block_children(page_id)
        for b in existing:
            try:
                await self.delete_block(b["id"])
            except Exception:
                pass
        return await self.append_blocks(page_id, blocks)
