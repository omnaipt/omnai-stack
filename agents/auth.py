"""Autenticacao simples por token de header."""
import os
from fastapi import Header, HTTPException

OMNAI_API_TOKEN = os.getenv("OMNAI_API_TOKEN", "")


async def require_token(
    x_omnai_token: str = Header(default="", alias="X-OMNAI-Token"),
) -> None:
    if not OMNAI_API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="OMNAI_API_TOKEN nao configurado no servidor",
        )
    if x_omnai_token != OMNAI_API_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Header X-OMNAI-Token em falta ou invalido",
        )
