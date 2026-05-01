"""Worker: alerta-fecho-contabilistico-dia1 | Ana | CFO

Cron: dia 1 de cada mes as 09:00 UTC.
Abre o ciclo mensal, cria tarefas e envia email com checklist.

Sprint 5: emite 1 card P0 por empresa em briefing_items para sinalizar
que o ciclo de fecho contabilistico do mes anterior arrancou.

Sprint 7.5: filtra empresas pelo escopo de responsabilidade contabilistica
do David (OMNAI, Sopato, Pessoal). Previnsa e JMSoares ficam de fora dos
cards de briefing porque a contabilidade dessas empresas e gerida por
outras entidades. As tarefas Notion e o email mantem-se inalterados.
"""
from __future__ import annotations

from datetime import date, timedelta

import structlog

from services import notion_ext
from services.briefing_emit import emit_briefing, page_url_from_id
from services.email_notify import send_notification
from services.notion_ext import paragraph
from services.responsabilidade_david import (
    EMPRESAS_CONTABILIDADE_DAVID,
    is_alerta_relevante,
)

log = structlog.get_logger()


TAREFAS_DB = "0135c7f8-2823-4287-9556-7968fd36998d"
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
EMPRESAS = ["OMNAI", "Previnsa", "JMSoares", "Sopato"]

# Sprint 7.5: subset de empresas que recebem card no briefing (David e
# responsavel pela contabilidade destas). Tarefas Notion + email continuam
# a ser geradas para todas as empresas em EMPRESAS.
EMPRESAS_PARA_BRIEFING = sorted(EMPRESAS_CONTABILIDADE_DAVID)

WORKER_NAME = "alerta-fecho-contabilistico-dia1"
TIPO = "fecho_contabilistico"

MONTH_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Marco", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _mes_anterior() -> tuple[int, int]:
    hoje = date.today()
    primeiro = date(hoje.year, hoje.month, 1)
    ultimo = primeiro - timedelta(days=1)
    return ultimo.year, ultimo.month


def _tarefas(ano: int, mes: int) -> list[str]:
    mes_nome = MONTH_PT[mes]
    base = f"[Fecho {mes_nome}/{ano}]"
    out = [f"{base} Compilar faturas {emp} (receitas + despesas)" for emp in EMPRESAS]
    out += [
        f"{base} Pedir extractos bancarios a todos os bancos",
        f"{base} Reconciliar movimentos bancarios vs faturas",
        f"{base} Verificar movimentos sem documento de suporte",
        f"{base} Enviar pacote contabilistico a contabilidade",
    ]
    return out


def _html(ano: int, mes: int, tarefas: list[str]) -> str:
    mes_nome = MONTH_PT[mes]
    items = "".join(f"<li>{t}</li>" for t in tarefas)
    return f"""<!DOCTYPE html>
<html><body style="font-family:system-ui;color:#111;">
<h2>Fecho Contabilistico {mes_nome} {ano}</h2>
<p>Inicia hoje o ciclo do mes anterior. Foram criadas {len(tarefas)} tarefas na DB Tarefas.</p>
<h3>Checklist</h3>
<ol>{items}</ol>
<h3>Regras OMNAI</h3>
<ul>
<li>Dia 3: pedir extractos bancarios</li>
<li>Dia 5-8: reconciliar</li>
<li>Dia 9: alerta se pacote nao pronto</li>
<li>Dia 10: envio a contabilidade</li>
</ul>
<p style="color:#555;font-size:12px;">Ana | CFO | OMNAI Agent Team</p>
</body></html>"""


async def _emitir_cards_fecho(ano: int, mes: int, activity_log_page_id: str | None) -> int:
    """1 card P0 por empresa, chave estavel (empresa, year_month).

    Sprint 7.5: itera apenas EMPRESAS_PARA_BRIEFING (OMNAI, Sopato, Pessoal).
    """
    mes_nome = MONTH_PT[mes]
    ym = f"{ano}-{mes:02d}"
    detalhe_base = (
        f"Ciclo de fecho contabilistico {mes_nome}/{ano} arranca hoje (dia 1).\n"
        "Calendario: dia 3 extractos, dia 5-8 reconciliar, dia 9 verificar, dia 10 enviar."
    )
    link = page_url_from_id(activity_log_page_id)
    n = 0
    for emp in EMPRESAS_PARA_BRIEFING:
        if not is_alerta_relevante(tipo=TIPO, empresa=emp):
            log.debug(
                "skip emit_briefing",
                tipo=TIPO,
                empresa=emp,
                reason="fora_responsabilidade_david",
            )
            continue
        ok = await emit_briefing(
            tipo=TIPO,
            titulo=f"Fecho {mes_nome}/{ano} | {emp}: ciclo aberto",
            detalhe=detalhe_base,
            urgencia="P0",
            empresa=emp,
            chave_parts=(emp, ym),
            link_origem=link,
            metadata={
                "ano": ano,
                "mes": mes,
                "year_month": ym,
                "fase": "abertura",
            },
            worker_name=WORKER_NAME,
        )
        if ok:
            n += 1
    return n


async def run() -> dict:
    ano, mes = _mes_anterior()
    tarefas = _tarefas(ano, mes)

    criadas = 0
    for t in tarefas:
        pid = await notion_ext.create_database_row(data_source_id=TAREFAS_DB, title=t)
        if pid:
            criadas += 1

    activity_id = await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=f"Inicio ciclo fecho contabilistico {MONTH_PT[mes]} {ano}",
        children=[paragraph(f"Criadas {criadas}/{len(tarefas)} tarefas. Email enviado.")],
    )

    cards_emitidos = await _emitir_cards_fecho(ano, mes, activity_id)

    email_ok = (await send_notification(
        subject=f"[OMNAI] Fecho contabilistico {MONTH_PT[mes]}/{ano} checklist",
        html_body=_html(ano, mes, tarefas),
    )) is not None

    out = {
        "status": "ok",
        "mes": f"{MONTH_PT[mes]} {ano}",
        "tarefas_criadas": criadas,
        "cards_emitidos": cards_emitidos,
        "empresas_briefing": EMPRESAS_PARA_BRIEFING,
        "email_enviado": email_ok,
    }
    log.info("alerta-fecho-contabilistico-dia1", **out)
    return out
