"""Worker: fecho-pacote | Ana | CFO. 11-09-2026.

Monta o pacote mensal para a contabilidade sem passar pelo Claude:

  1. Facturas validadas da empresa no mes (tabela faturas) -> copiadas para
     a pasta Drive <Faturas OMNAI>/<Empresa>/Fecho/<YYYY-MM>/.
  2. Facturas por validar e facturas com aviso (nome pessoal, NIF errado)
     -> listadas no resumo, nao vao no pacote.
  3. Extrato Revolut do mes (movimentos_bancarios) -> CSV na mesma pasta,
     com a categoria de cada movimento.
  4. Despesas do mes sem factura casada -> lista "por justificar".
  5. Transferencias internas (Main -> Poupanca) e para o socio -> lista a
     parte, porque a contabilista pergunta sempre por elas.
  6. Documentos emitidos na Moloni no mes (facturas de venda) -> PDFs na
     pasta. O SAF-T nao tem endpoint na API da Moloni: fica como passo
     manual, e o worker verifica se ja esta na pasta (saft*.xml).
  7. Resumo em texto (pacote_resumo.txt) na pasta e rascunho de email na
     caixa OMNAI para a contabilidade, com o link da pasta. Nunca envia.

Cron: dia 5 as 08:00 (depois do revolut-sync das 07:30). Reexecutavel: os
uploads nao duplicam pelo nome e o rascunho so e criado se nao existir um
igual em rascunhos. Pode ser chamado com mes='YYYY-MM' pela PWA ou pelo MCP.

Config opcional em /secrets/fecho_pacote.json:
  {"OMNAI": {"contabilidade": ["geral@eugest.pt", "ines.pereira@eugest.pt"],
             "conta_gmail": "david.sardinha@omnai.pt"}}
Sem config, o rascunho e criado na caixa OMNAI para geral@eugest.pt.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

WORKER_NAME = "fecho-pacote"
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
CONFIG_FILE = SECRETS_DIR / "fecho_pacote.json"

DEFAULT_CONFIG = {
    "OMNAI": {
        "contabilidade": ["geral@eugest.pt"],
        "conta_gmail": "david.sardinha@omnai.pt",
        "nif": "519270592",
    },
}

MES_PT = {1: "Janeiro", 2: "Fevereiro", 3: "Marco", 4: "Abril", 5: "Maio", 6: "Junho",
          7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"}


def config() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {**DEFAULT_CONFIG, **cfg}
    except FileNotFoundError:
        return DEFAULT_CONFIG
    except Exception as exc:
        log.warning("fecho_pacote.config_invalida", err=str(exc))
        return DEFAULT_CONFIG


def mes_anterior(hoje: date | None = None) -> str:
    hoje = hoje or date.today()
    ultimo = date(hoje.year, hoje.month, 1) - timedelta(days=1)
    return f"{ultimo.year}-{ultimo.month:02d}"


def _mes_nome(mes: str) -> str:
    y, m = mes.split("-")
    return f"{MES_PT[int(m)]} {y}"


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


# ---------------------------------------------------------------- recolha

async def recolher(empresa: str, mes: str) -> dict:
    """Junta tudo o que o pacote precisa, sem tocar no Drive nem no Gmail."""
    from services import faturas_db

    por_validar = await faturas_db.listar(mes=mes, empresa=empresa, estado="por_validar", limit=200)
    entregues = await faturas_db.listar(mes=mes, empresa=empresa, estado="entregue", limit=500)
    todas = await faturas_db.listar(mes=mes, empresa=empresa, estado="todos", limit=500)
    # Uma factura validada com aviso nao vai no pacote: ou parece recibo
    # (vai na lista de excluidos) ou esta em nome errado (lista de problemas).
    recibos_suspeitos = [f for f in todas if f.get("estado") == "validada"
                         and (f.get("aviso") or "").startswith("parece recibo")]
    com_aviso = [f for f in todas if f.get("aviso") and f.get("estado") in ("validada", "por_validar")
                 and not (f.get("aviso") or "").startswith("parece recibo")]
    excluir = {str(f["id"]) for f in recibos_suspeitos} | {str(f["id"]) for f in com_aviso}
    validadas = [f for f in await faturas_db.para_pacote(empresa, mes) if str(f["id"]) not in excluir]

    movimentos: list[dict] = []
    sem_fatura: list[dict] = []
    internas: list[dict] = []
    socio: list[dict] = []
    revolut_ok = False
    if empresa == "OMNAI":
        try:
            from services import movimentos_db
            movimentos = await movimentos_db.listar(mes=mes, limit=2000)
            revolut_ok = True
            for m in movimentos:
                if m["categoria"] == "interno":
                    internas.append(m)
                elif m["categoria"] == "despesa" and m["match_estado"] in ("por_casar", "sem_fatura"):
                    if (m.get("nota") or "").startswith("socio") or \
                            "sardinha" in ((m.get("contraparte") or "") + " " + (m.get("descricao") or "")).lower():
                        socio.append(m)
                    else:
                        sem_fatura.append(m)
        except Exception as exc:
            log.warning("fecho_pacote.revolut_indisponivel", err=str(exc))

    moloni_docs: list[dict] = []
    moloni_ok = False
    if empresa == "OMNAI":
        try:
            from services import moloni
            if moloni.configured():
                moloni_docs = await asyncio.to_thread(moloni.documents, mes)
                moloni_ok = True
        except Exception as exc:
            log.warning("fecho_pacote.moloni_indisponivel", err=str(exc))

    return {
        "empresa": empresa, "mes": mes,
        "validadas": validadas, "por_validar": por_validar, "entregues": entregues,
        "com_aviso": com_aviso, "recibos_suspeitos": recibos_suspeitos,
        "movimentos": movimentos, "sem_fatura": sem_fatura, "internas": internas,
        "socio": socio, "revolut_ok": revolut_ok,
        "moloni_docs": moloni_docs, "moloni_ok": moloni_ok,
    }


# ---------------------------------------------------------------- textos

def csv_movimentos(movs: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["data", "conta", "tipo", "valor", "moeda", "valor_original", "moeda_original",
                "contraparte", "descricao", "categoria", "estado_conciliacao", "fatura", "nota"])
    for m in sorted(movs, key=lambda x: (x["data"], x.get("conta_nome") or "")):
        w.writerow([m["data"], m.get("conta_nome"), m.get("tipo"), _fmt(m["valor"]), m["moeda"],
                    _fmt(m["valor_orig"]) if m.get("valor_orig") is not None else "",
                    m.get("moeda_orig") or "", m.get("contraparte") or "", m.get("descricao") or "",
                    m["categoria"], m["match_estado"], m.get("fatura_ficheiro") or "",
                    m.get("nota") or ""])
    return buf.getvalue().encode("utf-8-sig")


def _quem(m: dict) -> str:
    """Contraparte legivel: o Revolut devolve por vezes so o id da contraparte."""
    cp = m.get("contraparte") or ""
    if not cp or (len(cp) >= 32 and "-" in cp and cp.replace("-", "").isalnum()):
        return m.get("descricao") or cp or ""
    return cp


def resumo_texto(d: dict, link_pasta: str | None, saft_presente: bool) -> str:
    emp, mes = d["empresa"], d["mes"]
    L: list[str] = []
    L.append(f"PACOTE DE FECHO {emp} | {_mes_nome(mes)}")
    L.append(f"Gerado automaticamente pela app de gestao OMNAI em {date.today().isoformat()}.")
    if link_pasta:
        L.append(f"Pasta: {link_pasta}")
    L.append("")

    L.append(f"1. FACTURAS DE DESPESA VALIDADAS ({len(d['validadas'])})")
    tot: dict[str, float] = {}
    for f in d["validadas"]:
        tot[f["moeda"]] = tot.get(f["moeda"], 0) + float(f["valor"] or 0)
        L.append(f"   {f['data_fatura']}  {(f['fornecedor'] or '?')[:28]:<28} {_fmt(f['valor']):>10} {f['moeda']}  "
                 f"{f.get('referencia') or ''}  [{Path(f['ficheiro']).name}]")
    if tot:
        L.append("   Totais: " + ", ".join(f"{_fmt(v)} {k}" for k, v in sorted(tot.items())))
    if d["entregues"]:
        L.append(f"   (+{len(d['entregues'])} ja entregues num pacote anterior deste mes)")
    L.append("")

    if d["com_aviso"]:
        L.append(f"2. FACTURAS COM PROBLEMA DE DESTINATARIO ({len(d['com_aviso'])}) - NAO ENVIAR SEM CORRIGIR")
        for f in d["com_aviso"]:
            L.append(f"   {f['data_fatura']}  {(f['fornecedor'] or '?')[:28]:<28} {_fmt(f['valor']):>10} {f['moeda']}  -> {f['aviso']}")
        L.append("")
    if d["recibos_suspeitos"]:
        L.append(f"2b. RECIBOS DE PAGAMENTO EXCLUIDOS DO PACOTE ({len(d['recibos_suspeitos'])}); a factura correspondente vai acima")
        for f in d["recibos_suspeitos"]:
            L.append(f"   {f['data_fatura']}  {(f['fornecedor'] or '?')[:28]:<28} {_fmt(f['valor']):>10} {f['moeda']}  [{Path(f['ficheiro']).name}]")
        L.append("")
    if d["por_validar"]:
        L.append(f"3. FACTURAS POR VALIDAR NA APP ({len(d['por_validar'])}) - ficam de fora ate serem validadas")
        for f in d["por_validar"]:
            L.append(f"   {f['data_fatura']}  {(f['fornecedor'] or '?')[:28]:<28} {_fmt(f['valor']):>10} {f['moeda']}")
        L.append("")

    if d["revolut_ok"]:
        L.append(f"4. EXTRATO REVOLUT: {len(d['movimentos'])} movimentos no mes (extrato_revolut_{mes}.csv)")
        L.append("   O PDF oficial do extrato tem de ser descarregado da app Revolut (a API nao o fornece).")
        if d["internas"]:
            L.append(f"   Transferencias internas entre contas OMNAI ({len(d['internas'])}), nao sao despesa nem receita:")
            for m in d["internas"]:
                L.append(f"     {m['data']}  {_fmt(m['valor']):>10} {m['moeda']}  {m.get('conta_nome') or ''}  {m.get('descricao') or ''}")
        if d["socio"]:
            L.append(f"   Transferencias para o socio ({len(d['socio'])}), precisam de mapa de despesas/km:")
            for m in d["socio"]:
                L.append(f"     {m['data']}  {_fmt(m['valor']):>10} {m['moeda']}  {m.get('contraparte') or ''}  {m.get('nota') or ''}")
        if d["sem_fatura"]:
            L.append(f"   DESPESAS SEM FACTURA CASADA ({len(d['sem_fatura'])}) - obter documento ou justificar:")
            for m in d["sem_fatura"]:
                orig = f" ({_fmt(m['valor_orig'])} {m['moeda_orig']})" if m.get("valor_orig") else ""
                L.append(f"     {m['data']}  {_fmt(m['valor']):>10} {m['moeda']}{orig}  {_quem(m)[:40]}")
        L.append("")
    else:
        L.append("4. EXTRATO REVOLUT: indisponivel (sync nao correu ou Revolut nao ligado)")
        L.append("")

    if d["moloni_ok"]:
        L.append(f"5. FACTURACAO EMITIDA (Moloni): {len(d['moloni_docs'])} documentos")
        for doc in d["moloni_docs"]:
            L.append(f"   {doc['data']}  {doc['numero']:<12} {(doc['cliente'] or '')[:30]:<30} {_fmt(doc['total']):>10} EUR")
        L.append("   SAF-T do mes: " + ("na pasta." if saft_presente else
                 "EM FALTA. Exportar na Moloni (Contabilidade > SAF-T) e colocar na pasta; a API nao o exporta."))
    else:
        L.append("5. FACTURACAO EMITIDA: Moloni indisponivel")
    L.append("")
    L.append("Faltas a fechar antes de enviar: " + ", ".join(x for x in [
        f"{len(d['com_aviso'])} factura(s) em nome errado" if d["com_aviso"] else "",
        f"{len(d['por_validar'])} por validar" if d["por_validar"] else "",
        f"{len(d['sem_fatura'])} despesa(s) sem factura" if d["sem_fatura"] else "",
        f"{len(d['socio'])} transferencia(s) ao socio sem mapa" if d["socio"] else "",
        "" if saft_presente or not d["moloni_ok"] else "SAF-T",
        "extrato PDF Revolut",
    ] if x))
    return "\n".join(L)


def email_texto(d: dict, link_pasta: str | None, saft_presente: bool) -> str:
    emp, mes = d["empresa"], d["mes"]
    n = len(d["validadas"])
    linhas = [
        "Boa tarde,",
        "",
        f"Segue o pacote de {_mes_nome(mes)} da {emp}.",
        "",
        f"Pasta partilhada com tudo: {link_pasta or '(link da pasta)'}",
        "",
        f"- {n} facturas de despesa (lista e totais em pacote_resumo.txt)",
    ]
    if d["revolut_ok"]:
        linhas.append(f"- Extrato Revolut de {_mes_nome(mes)} (CSV com todos os movimentos e categoria; PDF em anexo)")
        if d["internas"]:
            linhas.append(f"- {len(d['internas'])} transferencia(s) interna(s) entre a conta a ordem e a Poupanca Omnai, ambas da empresa; nao sao despesa")
    if d["moloni_ok"]:
        linhas.append(f"- {len(d['moloni_docs'])} documento(s) emitido(s) na Moloni" + ("" if saft_presente else " (SAF-T segue assim que exportar)"))
    if d["sem_fatura"]:
        linhas.append(f"- {len(d['sem_fatura'])} movimento(s) ainda sem documento; estou a tratar e envio em separado")
    linhas += ["", "Qualquer coisa que falte, digam.", "", "Cumprimentos,", "David Sardinha"]
    return "\n".join(linhas)


# ---------------------------------------------------------------- pacote

async def montar(empresa: str = "OMNAI", mes: str | None = None, criar_rascunho: bool = True,
                 subir_drive: bool = True) -> dict:
    mes = mes or mes_anterior()
    cfg = config().get(empresa) or DEFAULT_CONFIG["OMNAI"]
    d = await recolher(empresa, mes)
    out: dict[str, Any] = {"empresa": empresa, "mes": mes, "validadas": len(d["validadas"]),
                           "por_validar": len(d["por_validar"]), "com_aviso": len(d["com_aviso"]),
                           "sem_fatura": len(d["sem_fatura"]), "internas": len(d["internas"]),
                           "socio": len(d["socio"]), "moloni_docs": len(d["moloni_docs"]),
                           "ficheiros_subidos": 0, "saft_presente": False, "link_pasta": None}

    link_pasta = None
    saft_presente = False
    if subir_drive:
        try:
            from services import drive
            svc = drive._service()
            pasta = drive.ensure_path([empresa, "Fecho", mes], svc=svc)
            link_pasta = drive.folder_link(pasta)
            out["link_pasta"] = link_pasta
            existentes = {f["name"] for f in drive.list_folder(pasta, svc=svc)}
            saft_presente = any(n.lower().startswith("saft") and n.lower().endswith(".xml") for n in existentes)
            subidos = 0
            for f in d["validadas"]:
                caminho = FATURAS_ROOT / f["ficheiro"]
                nome = caminho.name
                if nome in existentes or not caminho.is_file():
                    continue
                drive.upload_bytes_to(pasta, caminho.read_bytes(), nome, svc=svc)
                subidos += 1
            if d["revolut_ok"]:
                drive.upload_bytes_to(pasta, csv_movimentos(d["movimentos"]),
                                      f"extrato_revolut_{mes}.csv", "text/csv", svc=svc)
            if d["moloni_ok"]:
                from services import moloni
                for doc in d["moloni_docs"]:
                    nome = f"moloni_{doc['numero'].replace('/', '-')}_{doc['data']}.pdf"
                    if nome in existentes:
                        continue
                    try:
                        url = await asyncio.to_thread(moloni.document_pdf_link, int(doc["document_id"]))
                        if url:
                            import httpx
                            def _dl(u=url):
                                with httpx.Client(timeout=30.0, follow_redirects=True) as c:
                                    return c.get(u).content
                            data = await asyncio.to_thread(_dl)
                            if data[:4] == b"%PDF":
                                drive.upload_bytes_to(pasta, data, nome, svc=svc)
                                subidos += 1
                    except Exception as exc:
                        log.warning("fecho_pacote.moloni_pdf_falhou", doc=doc["numero"], err=str(exc))
            # O resumo e reescrito de cada vez: apaga o antigo primeiro.
            for f in drive.list_folder(pasta, svc=svc):
                if f["name"] == "pacote_resumo.txt":
                    svc.files().delete(fileId=f["id"]).execute()
            drive.upload_bytes_to(pasta, resumo_texto(d, link_pasta, saft_presente).encode("utf-8"),
                                  "pacote_resumo.txt", "text/plain", svc=svc)
            out["ficheiros_subidos"] = subidos
            out["saft_presente"] = saft_presente
        except Exception as exc:
            log.warning("fecho_pacote.drive_falhou", err=str(exc))
            out["drive_erro"] = f"{type(exc).__name__}: {exc}"

    out["resumo"] = resumo_texto(d, link_pasta, saft_presente)

    if criar_rascunho:
        try:
            from services import gmail
            conta = cfg.get("conta_gmail", "david.sardinha@omnai.pt")
            assunto = f"Pacote {_mes_nome(mes)} {empresa}"
            if not await asyncio.to_thread(_rascunho_existe, conta, assunto):
                rasc = await asyncio.to_thread(
                    gmail.create_draft, conta, ", ".join(cfg.get("contabilidade", [])),
                    assunto, email_texto(d, link_pasta, saft_presente))
                out["rascunho_id"] = rasc.get("id")
            else:
                out["rascunho_id"] = "ja_existia"
        except Exception as exc:
            log.warning("fecho_pacote.rascunho_falhou", err=str(exc))
            out["rascunho_erro"] = f"{type(exc).__name__}: {exc}"

    return out


def _rascunho_existe(conta: str, assunto: str) -> bool:
    from services import gmail
    svc = gmail._service(conta)
    r = svc.users().drafts().list(userId="me", q=f'subject:"{assunto}"', maxResults=5).execute()
    return bool(r.get("drafts"))


async def run() -> dict:
    mes = mes_anterior()
    resultados = {}
    for empresa in config().keys():
        try:
            resultados[empresa] = await montar(empresa, mes)
        except Exception as exc:
            log.warning("fecho_pacote.falhou", empresa=empresa, err=str(exc))
            resultados[empresa] = {"erro": f"{type(exc).__name__}: {exc}"}
    for empresa, r in resultados.items():
        if "erro" in r:
            continue
        faltas = [x for x in [
            f"{r['com_aviso']} em nome errado" if r["com_aviso"] else "",
            f"{r['por_validar']} por validar" if r["por_validar"] else "",
            f"{r['sem_fatura']} sem factura" if r["sem_fatura"] else "",
            "" if r["saft_presente"] else "SAF-T",
        ] if x]
        try:
            from services.briefing_emit import emit_briefing
            await emit_briefing(
                tipo="fecho_pacote",
                titulo=f"Pacote {_mes_nome(mes)} {empresa}: {r['validadas']} facturas, "
                       + (f"faltam {', '.join(faltas)}" if faltas else "completo"),
                detalhe=r.get("resumo", "")[:1800],
                urgencia="P1" if faltas else "P2", empresa=empresa,
                chave_parts=(empresa, mes), link_origem=r.get("link_pasta"),
                metadata={"mes": mes, "rascunho_id": r.get("rascunho_id")},
                worker_name=WORKER_NAME)
        except Exception as exc:
            log.warning("fecho_pacote.card_falhou", err=str(exc))
    return {"status": "ok", "mes": mes, "empresas": {k: {kk: vv for kk, vv in v.items() if kk != "resumo"}
                                                     for k, v in resultados.items()}}
