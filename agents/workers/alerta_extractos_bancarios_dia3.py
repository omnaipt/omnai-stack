"""Worker: alerta-extractos-bancarios-dia3 | Ana | CFO

Cron: dia 3 de cada mes 09:00 UTC.
Lembrete ao David para descarregar extractos das contas de cada empresa.

Sprint 5: emite 1 card P1 por (empresa, banco) em briefing_items, com
chave estavel (empresa, banco, year_month).

Sprint 7.5: filtra por responsabilidade contabilistica do David. Cards
extracto_bancario_pend so para OMNAI, Sopato e Pessoal. Email + Notion
mantem-se inalterados.
"""
from __future__ import annotations

import json
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


CONFIG_BANCOS = Path(os.getenv("BANCOS_JSON", "/app/agents/config/bancos.json"))
ACTIVITY_LOG_DB = "127f34a9-d97d-40ed-9fea-d4acb5cd2b31"

WORKER_NAME = "alerta-extractos-bancarios-dia3"
TIPO = "extracto_bancario_pend"

EMPRESAS_CANONICAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")

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


def _carregar() -> dict:
    if not CONFIG_BANCOS.exists():
        log.warning("bancos.json nao encontrado", path=str(CONFIG_BANCOS))
        return {}
    return json.loads(CONFIG_BANCOS.read_text(encoding="utf-8"))


def _normalizar_empresa(raw: str) -> str:
    if not raw:
        return "OMNAI"
    s = raw.strip()
    return s if s in EMPRESAS_CANONICAS else "OMNAI"


def _slug(text: str) -> str:
    s = (text or "").strip().lower()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "banco"


def _html(ano: int, mes: int, bancos: dict) -> str:
    mes_nome = MONTH_PT[mes]
    seccoes: list[str] = []
    for empresa, contas in bancos.items():
        if empresa.startswith("_") or not contas:
            continue
        lis = "".join(
            f"<li><b>{c['banco']}</b> | {c.get('conta', '')} "
            f"<span style='color:#777'>({c.get('obs', '')})</span></li>"
            for c in contas
        )
        seccoes.append(f"<h3>{empresa}</h3><ul>{lis}</ul>")
    corpo = "\n".join(seccoes) or "<p><i>Sem bancos em bancos.json.</i></p>"

    q = (mes - 1) // 3 + 1
    return f"""<!DOCTYPE html>
<html><body style="font-family:system-ui;color:#111;">
<h2>Extractos bancarios {mes_nome} {ano}</h2>
<p>Lembrete: descarregar extractos do mes anterior.</p>
<p>Arquivar em <code>/faturas/&lt;Empresa&gt;/{ano}-Q{q}/extractos/</code></p>
{corpo}
<h3>Checklist</h3>
<ol>
<li>Descarregar PDF de cada banco</li>
<li>Upload Google Drive pasta Extractos/&lt;Empresa&gt;</li>
<li>Marcar tarefa "Pedir extractos" como Done</li>
</ol>
<p style="color:#555;font-size:12px;">Ana | CFO | OMNAI Agent Team</p>
</body></html>"""


async def _emitir_cards_extractos(ano: int, mes: int, bancos: dict, activity_log_page_id: str | None) -> int:
    """1 card P1 por (empresa, banco), chave (empresa, banco_slug, year_month).

    Sprint 7.5: filtra por is_alerta_relevante. Empresas com contabilidade
    gerida externamente (Previnsa, JMSoares) sao silenciosamente skipadas.
    """
    mes_nome = MONTH_PT[mes]
    ym = f"{ano}-{mes:02d}"
    link = page_url_from_id(activity_log_page_id)
    n = 0
    for empresa_raw, contas in bancos.items():
        if empresa_raw.startswith("_") or not contas:
            continue
        empresa = _normalizar_empresa(empresa_raw)
        if not is_alerta_relevante(tipo=TIPO, empresa=empresa):
            log.debug(
                "skip emit_briefing",
                tipo=TIPO,
                empresa=empresa,
                reason="fora_responsabilidade_david",
            )
            continue
        for c in contas:
            banco = (c.get("banco") or "").strip() or "(sem nome)"
            conta = (c.get("conta") or "").strip()
            obs = (c.get("obs") or "").strip()
            banco_slug = _slug(banco)
            titulo = f"Extracto bancario pendente: {empresa} | {banco}"
            detalhe = (
                f"Mes: {mes_nome} {ano}\n"
                f"Banco: {banco}\n"
                f"Conta: {conta or '-'}\n"
                f"Notas: {obs or '-'}\n"
                "Accao: descarregar PDF do banco e arquivar em /faturas/<Empresa>/<Ano>-Q<N>/extractos/"
            )
            ok = await emit_briefing(
                tipo=TIPO,
                titulo=titulo[:180],
                detalhe=detalhe,
                urgencia="P1",
                empresa=empresa,
                chave_parts=(empresa, banco_slug, ym),
                link_origem=link,
                metadata={
                    "ano": ano,
                    "mes": mes,
                    "year_month": ym,
                    "banco": banco,
                    "conta": conta,
                    "obs": obs,
                },
                worker_name=WORKER_NAME,
            )
            if ok:
                n += 1
    return n


async def run() -> dict:
    ano, mes = _mes_anterior()
    bancos = _carregar()
    total = sum(len(v) for k, v in bancos.items() if not k.startswith("_"))

    email_ok = (await send_notification(
        subject=f"[OMNAI] Lembrete extractos bancarios {MONTH_PT[mes]}/{ano}",
        html_body=_html(ano, mes, bancos),
    )) is not None

    activity_id = await notion_ext.create_database_row(
        data_source_id=ACTIVITY_LOG_DB,
        title=f"Lembrete extractos bancarios {MONTH_PT[mes]} {ano}",
        children=[paragraph(f"Lembrete para {total} contas.")],
    )

    cards_emitidos = await _emitir_cards_extractos(ano, mes, bancos, activity_id)

    out = {
        "status": "ok",
        "mes": f"{MONTH_PT[mes]} {ano}",
        "contas_total": total,
        "cards_emitidos": cards_emitidos,
        "email_enviado": email_ok,
    }
    log.info("alerta-extractos-bancarios-dia3", **out)
    return out
