"""Worker: alerta-deadline-contabilidade-dia9 | Ana | CFO

Cron: dia 9 de cada mes 09:00 UTC.
Verifica se pacote contabilistico esta pronto (conta PDFs em /faturas/<Empresa>/<Ano>-QN/).

Sprint 5: emite 1 card por empresa em briefing_items. Empresas em ATENCAO
recebem P0; empresas OK recebem P2 informativo (com chave estavel por
year_month, idempotente).

Sprint 7.5: filtra por responsabilidade contabilistica do David. Cards
deadline_contabilidade so para OMNAI, Sopato (e Pessoal se tiver minimo
configurado). Email + tarefa Notion + activity log mantem-se inalterados.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import structlog

from services import notion_ext
from services.briefing_emit import emit_briefing, page_url_from_id
from services.email_notify import send_notification
from services.notion_ext import paragraph
from services.responsabilidade_david import is_alerta_relevante

log = structlog.get_logger()


FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"
TAREFAS_DB = "0135c7f8-2823-4287-9556-7968fd36998d"

WORKER_NAME = "alerta-deadline-contabilidade-dia9"
TIPO = "deadline_contabilidade"

MIN_POR_EMPRESA = {"OMNAI": 5, "Previnsa": 10, "JMSoares": 3, "Sopato": 4}

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


def _quarter(mes: int) -> int:
    return (mes - 1) // 3 + 1


def _contar(empresa: str, ano: int, mes: int) -> int:
    pasta = FATURAS_ROOT / empresa / f"{ano}-Q{_quarter(mes)}"
    if not pasta.exists():
        return 0
    return sum(1 for p in pasta.iterdir() if p.suffix.lower() == ".pdf")


def _html(ano: int, mes: int, estado: dict, tudo_ok: bool) -> str:
    mes_nome = MONTH_PT[mes]
    linhas = []
    for emp, info in estado.items():
        cor = "#1b8a3a" if info["ok"] else "#b00020"
        linhas.append(
            f"<tr><td><b>{emp}</b></td>"
            f"<td style='color:{cor};'><b>{info['count']}</b> / min {info['minimo']}</td>"
            f"<td>{info['status']}</td></tr>"
        )
    tabela = (
        "<table style='border-collapse:collapse;width:100%;'>"
        "<thead style='background:#f0f0f0'><tr><th>Empresa</th><th>Faturas</th><th>Estado</th></tr></thead>"
        f"<tbody>{''.join(linhas)}</tbody></table>"
    )

    if tudo_ok:
        cabeca = "<h2 style='color:#1b8a3a;'>Pacote aparentemente completo</h2>"
        accao = "<p>Podes enviar amanha (dia 10).</p>"
    else:
        criticas = ", ".join(e for e, info in estado.items() if not info["ok"])
        cabeca = "<h2 style='color:#b00020;'>Pacote possivelmente incompleto</h2>"
        accao = f"<p>Revisao urgente para: <b>{criticas}</b>.</p>"

    return f"""<!DOCTYPE html>
<html><body style="font-family:system-ui;color:#111;">
{cabeca}
<p>Pacote contabilistico {mes_nome} {ano}</p>
{tabela}
{accao}
<p style="color:#555;font-size:12px;">Ana | CFO | OMNAI Agent Team</p>
</body></html>"""


async def _emitir_cards_estado(ano: int, mes: int, estado: dict, activity_log_page_id: str | None) -> int:
    """1 card por empresa: ATENCAO -> P0, OK -> P2.

    Chave estavel (empresa, year_month) garante idempotencia se o cron
    correr 2x ou se houver re-trigger manual no mesmo dia.

    Sprint 7.5: filtra por is_alerta_relevante.
    """
    mes_nome = MONTH_PT[mes]
    ym = f"{ano}-{mes:02d}"
    link = page_url_from_id(activity_log_page_id)
    n = 0
    for emp, info in estado.items():
        if not is_alerta_relevante(tipo=TIPO, empresa=emp):
            log.debug(
                "skip emit_briefing",
                tipo=TIPO,
                empresa=emp,
                reason="fora_responsabilidade_david",
            )
            continue
        ok_estado = info["ok"]
        urg = "P2" if ok_estado else "P0"
        marca = "OK" if ok_estado else "ATENCAO"
        titulo = f"Deadline contabilidade dia 9 {mes_nome}/{ano} | {emp}: {marca} ({info['count']}/{info['minimo']})"
        detalhe = (
            f"Empresa: {emp}\n"
            f"Mes: {mes_nome} {ano}\n"
            f"Pasta: /faturas/{emp}/{ano}-Q{_quarter(mes)}\n"
            f"Faturas encontradas: {info['count']} (minimo esperado {info['minimo']})\n"
            f"Estado: {info['status']}"
        )
        ok = await emit_briefing(
            tipo=TIPO,
            titulo=titulo[:180],
            detalhe=detalhe,
            urgencia=urg,
            empresa=emp,
            chave_parts=(emp, ym),
            link_origem=link,
            metadata={
                "ano": ano,
                "mes": mes,
                "year_month": ym,
                "count": info["count"],
                "minimo": info["minimo"],
                "ok": ok_estado,
            },
            worker_name=WORKER_NAME,
        )
        if ok:
            n += 1
    return n


async def run() -> dict:
    ano, mes = _mes_anterior()
    estado: dict = {}
    tudo_ok = True

    for emp, minimo in MIN_POR_EMPRESA.items():
        count = _contar(emp, ano, mes)
        ok = count >= minimo
        estado[emp] = {"count": count, "minimo": minimo, "ok": ok, "status": "OK" if ok else "ATENCAO"}
        if not ok:
            tudo_ok = False

    subject = f"[OMNAI] Pacote contabilistico {MONTH_PT[mes]}/{ano} " + ("pronto" if tudo_ok else "ALERTA")
    email_ok = (await send_notification(subject=subject, html_body=_html(ano, mes, estado, tudo_ok))) is not None

    if not tudo_ok:
        for emp, info in estado.items():
            if info["ok"]:
                continue
            await notion_ext.create_database_row(
                data_source_id=TAREFAS_DB,
                title=f"[Fecho {MONTH_PT[mes]}/{ano}] URGENTE: verificar faturas em falta {emp}",
            )

    summary = "; ".join(f"{e}: {info['count']}/{info['minimo']}" for e, info in estado.items())
    activity_id = await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=f"Alerta dia 9 {MONTH_PT[mes]}/{ano} | {'OK' if tudo_ok else 'ATENCAO'}",
        children=[paragraph(summary)],
    )

    cards_emitidos = await _emitir_cards_estado(ano, mes, estado, activity_id)

    out = {
        "status": "ok",
        "mes": f"{MONTH_PT[mes]} {ano}",
        "estado": estado,
        "tudo_ok": tudo_ok,
        "cards_emitidos": cards_emitidos,
        "email_enviado": email_ok,
    }
    log.info("alerta-deadline-contabilidade-dia9", **out)
    return out
