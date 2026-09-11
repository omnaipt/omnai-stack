# -*- coding: utf-8 -*-
"""Vigia do arquivo no Drive.

03-08-2026. O arquivo para o Drive esteve parado de 18 de Abril a 3 de
Agosto, dois meses e meio, e ninguem soube: o codigo apanhava a excepcao,
escrevia uma linha de log e devolvia a factura como arquivada.

04-08-2026: passou a usar services.avarias, que e o mecanismo geral. Este
modulo so sabe *como se ve* se o Drive esta bom; quem trata de tornar uma
falha visivel, e de a fechar quando passa, e o outro.
"""
from __future__ import annotations

import asyncio

import structlog

from services.avarias import reportar, resolvida

log = structlog.get_logger()

AREA = "arquivo:drive"


async def verificar() -> dict:
    from services import drive

    try:
        estado = await asyncio.to_thread(drive.test_connection)
    except Exception as exc:
        estado = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    if estado.get("ok"):
        await resolvida(AREA)
        return estado

    erro = str(estado.get("error", ""))[:300]
    await reportar(
        area=AREA,
        titulo="Arquivo de facturas no Drive parado",
        detalhe=(
            "As facturas continuam a ser guardadas no servidor, mas nao "
            "estao a subir para o Google Drive. Foi assim que o arquivo "
            "esteve parado dois meses e meio sem ninguem dar por isso.\n\n"
            "Erro devolvido pelo Google: " + erro + "\n\n"
            "Se disser invalid_grant, o token expirou e e preciso "
            "reautorizar a conta do Drive."
        ),
        urgencia="P1",
    )
    return estado
