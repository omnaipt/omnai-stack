"""Chamada ao modelo com texto e imagens (11-09-2026).

O services/llm.py so aceita texto. A contabilista manda as listas de facturas
em falta como imagens coladas no email; sem ver as imagens o pedido chega
vazio. Este modulo e o unico sitio do stack que envia imagens ao modelo.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from services.llm import MODEL, _get_client

MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGENS = 8
MAX_BYTES_IMAGEM = 4 * 1024 * 1024


def _bloco_imagem(dados: bytes, media_type: str) -> dict | None:
    if media_type not in MEDIA_TYPES or len(dados) > MAX_BYTES_IMAGEM or len(dados) < 200:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(dados).decode("ascii")}}


async def generate_multimodal(system: str, prompt: str, imagens: list[tuple[bytes, str]],
                              max_tokens: int = 2000, model: str | None = None) -> str:
    conteudo: list[dict[str, Any]] = []
    for dados, mt in imagens[:MAX_IMAGENS]:
        b = _bloco_imagem(dados, mt)
        if b:
            conteudo.append(b)
    conteudo.append({"type": "text", "text": prompt})
    msg = await _get_client().messages.create(
        model=model or MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": conteudo}])
    return "\n".join(b.text for b in msg.content if isinstance(getattr(b, "text", None), str)).strip()


def json_da_resposta(texto: str) -> Any:
    """Tira o JSON de uma resposta que pode vir com cerca de markdown."""
    s = texto.strip()
    if s.startswith("```"):
        s = "\n".join(s.split("\n")[1:])
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    a, b = s.find("{"), s.rfind("}")
    if a == -1:
        a, b = s.find("["), s.rfind("]")
    if a != -1 and b > a:
        s = s[a:b + 1]
    return json.loads(s)
