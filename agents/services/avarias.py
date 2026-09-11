# -*- coding: utf-8 -*-
"""Uma avaria tem de acabar num ecra, nao numa linha de log.

04-08-2026. Num so dia apanharam-se quatro falhas com a mesma forma: a
operacao ficava feita a meio, a metade que falhava escrevia um log que
ninguem le, e o ecra do David dizia que estava tudo bem.

  * o arquivo no Drive parou a 18 de Abril e ninguem soube durante dois
    meses e meio;
  * o scan de email devolvia status "ok" com todas as contas a falhar
    autenticacao;
  * a tabela de facturas ficou congelada a 31 de Julho sem indexador;
  * a extraccao de metadados falhava e o PDF evaporava-se.

Existem 103 blocos "except Exception: log.warning" neste codigo. Nao se
mexe nos 103. Mexe-se naqueles em que o silencio faz perder trabalho, e
esses passam a usar isto.

Duas propriedades que interessam:

  * **Uma avaria por area.** A chave e estavel, por isso ha um cartao e nao
    um por cada vez que corre. Um alarme que se repete todos os dias e um
    alarme que se aprende a ignorar.
  * **Fecha-se sozinha.** Quando a area volta a funcionar, o cartao fecha.
    Um alarme que so alguem consegue desligar a mao acaba por ficar aceso
    para sempre e deixa de querer dizer alguma coisa.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()

TIPO = "avaria_sistema"


async def reportar(area: str, titulo: str, detalhe: str = "",
                   urgencia: str = "P1", empresa: str = "OMNAI") -> None:
    """Torna visivel uma falha que de outra forma so existiria no log."""
    from services.briefing_emit import emit_briefing
    try:
        await emit_briefing(
            tipo=TIPO,
            titulo=titulo[:180],
            detalhe=detalhe[:2000] or None,
            urgencia=urgencia,
            empresa=empresa,
            chave_parts=(area,),
            worker_name="avarias",
        )
        log.warning("avaria.reportada", area=area, titulo=titulo[:80])
    except Exception as exc:
        # Uma falha a reportar falhas nao pode derrubar o worker.
        log.error("avaria.reportar_falhou", area=area, err=str(exc))


async def resolvida(area: str) -> None:
    """Fecha o cartao da area, se estiver aberto. Silencioso de proposito."""
    try:
        from services.briefing_db import _get_pool, make_chave
        chave = make_chave(TIPO, area)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            n = await conn.execute(
                "UPDATE briefing_items SET status='done', resolvido_em=now() "
                "WHERE chave=$1 AND status IN ('open','snoozed')", chave)
        if n.rsplit(" ", 1)[-1] != "0":
            log.info("avaria.resolvida", area=area)
    except Exception as exc:
        log.warning("avaria.fechar_falhou", area=area, err=str(exc))
