"""Wrapper fino da API da Anthropic."""
from __future__ import annotations

import os
from typing import Optional

from anthropic import AsyncAnthropic

_client: Optional[AsyncAnthropic] = None
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY nao definido")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


async def generate(
    system: str,
    prompt: str,
    max_tokens: int = 2048,
    model: str | None = None,
) -> str:
    msg = await _get_client().messages.create(
        model=model or MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in msg.content if hasattr(b, "text")]
    return "\n".join(parts).strip()
