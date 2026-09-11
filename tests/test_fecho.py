"""Testes do passo 2 (fecho-pacote, recibos, dedup, destinatario) e passo 3
(resposta-contabilidade), com servicos simulados."""
import asyncio
import json
import os
import sys
import types
import zlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(ROOT))
TMP = Path("/tmp/fecho_test")
import shutil
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir()
(TMP / "secrets").mkdir()
os.environ["SECRETS_DIR"] = str(TMP / "secrets")
os.environ["FATURAS_DIR"] = str(TMP / "faturas")
os.environ["DRIVE_ENABLED"] = "false"
os.environ.setdefault("DATABASE_URL", "postgresql://x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from services import doc_fiscal, invoices, faturas_index  # noqa: E402
from workers import fecho_pacote, resposta_contabilidade as rc  # noqa: E402


def pdf_com_texto(texto: str) -> bytes:
    """PDF minimo com uma pagina de texto Helvetica, legivel pelo pypdf."""
    linhas = texto.split("\n")
    conteudo = "BT /F1 10 Tf 40 780 Td 12 TL " + " ".join(
        "(%s) Tj T*" % l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for l in linhas) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        "<< /Length %d >>\nstream\n%s\nendstream" % (len(conteudo), conteudo),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (i, o)).encode("latin-1")
    xref = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode()
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)).encode()
    return out


# ------------------------------------------------------------ doc_fiscal

def test_recibo_vs_fatura():
    rec = "Receipt\nReceipt number 2655-0771-1374\nDate paid August 9, 2026\nInvoice number YHNGV4Y2-0035\nAmount paid EUR 38.25\nThanks for your business"
    fat = "Invoice\nInvoice number YHNGV4Y2-0035\nDate due August 9, 2026\nBill to Omnai Consulting Lda PT519270592\nAmount due EUR 38.25"
    assert doc_fiscal.e_recibo(rec)["e_recibo"] is True
    assert doc_fiscal.e_recibo(fat)["e_recibo"] is False
    # factura portuguesa com ATCUD e "recibo" no nome (fatura-recibo) nunca e recibo
    assert doc_fiscal.e_recibo("Fatura-Recibo FR M/338493 ATCUD: JFX2-338493 Recibo n 1 Total 160,88")["e_recibo"] is False


def test_destinatario():
    v = doc_fiscal.verificar_destinatario
    assert "nome pessoal" in v("Hostinger\nBill to: David Sardinha Alves\nNIF: 230791611\nTotal 17.99", "OMNAI")
    assert v("Bill to Omnai Consulting Lda VAT PT519270592 Total 20.00", "OMNAI") is None
    assert "NIF errado" in v("Resend Bill to Omnai Consulting, Lda VAT PT519570592", "OMNAI")
    assert v("Anthropic PBC Bill to Omnai Consulting Lda Portugal", "OMNAI") is None
    assert "sem NIF" in v("Google Workspace Invoice 6.80 EUR customer 123", "OMNAI")
    assert v("qualquer coisa", "Sopato") is None  # empresa sem NIF configurado: nao avisa


# ------------------------------------------------------------ invoices

def test_save_dedup_recibo_aviso():
    meta = {"supplier": "Anthropic", "date": "2026-08-09", "amount": 38.25,
            "currency": "EUR", "invoice_number": "YHNGV4Y2-0035"}
    fat = pdf_com_texto("Invoice\nInvoice number YHNGV4Y2-0035\nDate due August 9, 2026\nBill to Omnai Consulting Lda PT519270592\nAmount due EUR 38.25")
    r1 = invoices.save_invoice_pdf(fat, meta, "david.sardinha@omnai.pt")
    assert r1["filename"] == "anthropic_2026-08-09_38-25EUR_yhngv4y2-0035.pdf"
    assert r1["recibo"] is False and r1["aviso"] is None
    # mesmo PDF outra vez: nao grava, aponta para o existente
    r2 = invoices.save_invoice_pdf(fat, meta, "david.sardinha@omnai.pt")
    assert r2.get("duplicado_de") == "OMNAI/2026-Q3/anthropic_2026-08-09_38-25EUR_yhngv4y2-0035.pdf"
    assert not (TMP / "faturas/OMNAI/2026-Q3/anthropic_2026-08-09_38-25EUR_yhngv4y2-0035_1.pdf").exists()
    # recibo do mesmo email: vai para _recibos
    rec = pdf_com_texto("Receipt\nReceipt number 2655-0771-1374\nDate paid August 9, 2026\nInvoice number YHNGV4Y2-0035\nAmount paid EUR 38.25")
    r3 = invoices.save_invoice_pdf(rec, meta, "david.sardinha@omnai.pt")
    assert r3["recibo"] is True
    assert r3["path"].endswith("OMNAI/2026-Q3/_recibos/anthropic_2026-08-09_38-25EUR_yhngv4y2-0035_recibo.pdf")
    # factura em nome pessoal: aviso
    host = pdf_com_texto("Hostinger International Ltd\nInvoice number HI-1\nBill to: David Sardinha Alves\nNIF: 230791611\nTotal 17.99 EUR")
    r4 = invoices.save_invoice_pdf(host, {"supplier": "Hostinger", "date": "2026-07-02", "amount": 17.99,
                                          "currency": "EUR", "invoice_number": "HI-1"}, "david.sardinha@omnai.pt")
    assert r4["aviso"] and "230791611" in r4["aviso"]
    assert invoices.reconstruir_hashes() == 0  # ja estavam todos registados


def test_index_partes():
    assert faturas_index._partes("OMNAI/2026-Q3/_recibos/x.pdf") == ("OMNAI", "2026-Q3", "recibo")
    assert faturas_index._partes("OMNAI/2026-Q3/x.pdf") == ("OMNAI", "2026-Q3", None)


# ------------------------------------------------------------ fecho_pacote

FATURAS = [
    {"id": "f1", "empresa": "OMNAI", "mes": "2026-08", "fornecedor": "anthropic", "data_fatura": date(2026, 8, 7),
     "valor": Decimal("180.00"), "moeda": "EUR", "referencia": "yhngv4y2 0034", "ficheiro": "OMNAI/2026-Q3/anthropic_2026-08-07_180-00EUR_yhngv4y2-0034.pdf",
     "estado": "validada", "aviso": None, "motivo": None},
    {"id": "f2", "empresa": "OMNAI", "mes": "2026-08", "fornecedor": "hostinger", "data_fatura": date(2026, 8, 3),
     "valor": Decimal("17.99"), "moeda": "EUR", "referencia": None, "ficheiro": "OMNAI/2026-Q3/hostinger_2026-08-03_17-99EUR.pdf",
     "estado": "validada", "aviso": "factura em nome pessoal (David Sardinha, NIF 230791611); pedir reemissao para OMNAI NIF 519270592", "motivo": None},
    {"id": "f3", "empresa": "OMNAI", "mes": "2026-08", "fornecedor": "supabase", "data_fatura": date(2026, 8, 17),
     "valor": Decimal("59.02"), "moeda": "USD", "referencia": "iugqat 00008", "ficheiro": "OMNAI/2026-Q3/supabase_2026-08-17_59-02USD_iugqat-00008.pdf",
     "estado": "por_validar", "aviso": None, "motivo": None},
    {"id": "f4", "empresa": "OMNAI", "mes": "2026-08", "fornecedor": "anthropic", "data_fatura": date(2026, 8, 9),
     "valor": Decimal("38.25"), "moeda": "EUR", "referencia": "yhngv4y2 0035 recibo", "ficheiro": "OMNAI/2026-Q3/_recibos/anthropic_2026-08-09_38-25EUR_yhngv4y2-0035_recibo.pdf",
     "estado": "ignorada", "aviso": None, "motivo": "arquivado em recibo"},
]
MOVS = [
    {"id": "m1", "data": date(2026, 8, 7), "conta_nome": "Main", "tipo": "card_payment", "valor": Decimal("-180.00"), "moeda": "EUR",
     "valor_orig": None, "moeda_orig": None, "descricao": "Anthropic", "contraparte": "Anthropic", "categoria": "despesa",
     "match_estado": "casado", "nota": None, "fatura_ficheiro": "OMNAI/2026-Q3/anthropic_2026-08-07_180-00EUR_yhngv4y2-0034.pdf"},
    {"id": "m2", "data": date(2026, 8, 11), "conta_nome": "Main", "tipo": "transfer", "valor": Decimal("-691.20"), "moeda": "EUR",
     "valor_orig": None, "moeda_orig": None, "descricao": "To David", "contraparte": "DAVID SOARES SARDINHA ALVES", "categoria": "despesa",
     "match_estado": "sem_fatura", "nota": "socio: transferencia para o socio", "fatura_ficheiro": None},
    {"id": "m3", "data": date(2026, 8, 25), "conta_nome": "Main", "tipo": "card_payment", "valor": Decimal("-70.00"), "moeda": "EUR",
     "valor_orig": None, "moeda_orig": None, "descricao": "Indigo", "contraparte": "Indigo", "categoria": "despesa",
     "match_estado": "sem_fatura", "nota": None, "fatura_ficheiro": None},
    {"id": "m4", "data": date(2026, 8, 22), "conta_nome": "Main", "tipo": "transfer", "valor": Decimal("-1500.00"), "moeda": "EUR",
     "valor_orig": None, "moeda_orig": None, "descricao": "Main -> Poupança Omnai", "contraparte": None, "categoria": "interno",
     "match_estado": "nao_precisa", "nota": None, "fatura_ficheiro": None},
    {"id": "m5", "data": date(2026, 8, 31), "conta_nome": "Poupança Omnai", "tipo": "interest", "valor": Decimal("2.31"), "moeda": "EUR",
     "valor_orig": None, "moeda_orig": None, "descricao": "Interest", "contraparte": None, "categoria": "juros",
     "match_estado": "nao_precisa", "nota": None, "fatura_ficheiro": None},
]
MOLONI = [{"document_id": 9, "tipo": "FT", "numero": "M/3", "data": "2026-08-05", "cliente": "Previnsa", "nif": "5", "base": 1000, "total": 1230, "estado": 1}]


def _stub_services(monkeypatch):
    fdb = types.ModuleType("services.faturas_db")

    async def para_pacote(empresa, mes):
        return [{k: f[k] for k in ("id", "fornecedor", "data_fatura", "valor", "moeda", "referencia", "ficheiro")}
                for f in FATURAS if f["estado"] == "validada"]

    async def listar(mes=None, empresa=None, estado="por_validar", limit=200):
        return [f for f in FATURAS if estado == "todos" or f["estado"] == estado]

    async def marcar_entregues(ids):
        return len(ids)

    fdb.para_pacote, fdb.listar, fdb.marcar_entregues = para_pacote, listar, marcar_entregues
    mdb = types.ModuleType("services.movimentos_db")

    async def listar_m(mes=None, limit=500, **kw):
        return MOVS

    mdb.listar = listar_m
    mol = types.ModuleType("services.moloni")
    mol.configured = lambda: True
    mol.documents = lambda mes=None: MOLONI
    mol.document_pdf_link = lambda i: None
    import services
    for name, mod in (("faturas_db", fdb), ("movimentos_db", mdb), ("moloni", mol)):
        monkeypatch.setattr(services, name, mod, raising=False)
        monkeypatch.setitem(sys.modules, f"services.{name}", mod)


def test_fecho_pacote_resumo(monkeypatch):
    _stub_services(monkeypatch)
    r = asyncio.run(fecho_pacote.montar("OMNAI", "2026-08", criar_rascunho=False, subir_drive=False))
    # a Hostinger em nome pessoal e excluida do pacote e vai para a lista de problemas
    assert r["validadas"] == 1 and r["por_validar"] == 1 and r["com_aviso"] == 1
    assert r["sem_fatura"] == 1 and r["socio"] == 1 and r["internas"] == 1 and r["moloni_docs"] == 1
    txt = r["resumo"]
    assert "PACOTE DE FECHO OMNAI | Agosto 2026" in txt
    assert "hostinger" in txt and "230791611" in txt
    assert "Indigo" in txt and "1 500,00" in txt and "M/3" in txt
    assert "SAF-T" in txt and "EM FALTA" in txt
    assert "Totais: 180,00 EUR" in txt and "NAO ENVIAR SEM CORRIGIR" in txt
    d = asyncio.run(fecho_pacote.recolher("OMNAI", "2026-08"))
    csv_ = fecho_pacote.csv_movimentos(d["movimentos"]).decode("utf-8-sig")
    assert csv_.splitlines()[0].startswith("data;conta;tipo;valor") and "interno" in csv_
    email = fecho_pacote.email_texto(d, "https://drive/x", False)
    assert "1 facturas de despesa" in email and "SAF-T segue" in email
    assert fecho_pacote.mes_anterior(date(2026, 9, 11)) == "2026-08"
    assert fecho_pacote.mes_anterior(date(2026, 1, 3)) == "2025-12"


# ------------------------------------------------------------ resposta

def test_verificar_um():
    links = {"anthropic_2026-08-07_180-00EUR_yhngv4y2-0034.pdf": "https://drive/a"}
    p = {"tipo": "fatura_em_falta", "fornecedor": "Anthropic", "data": "2026-08-07", "valor": 180.0, "referencia": None}
    v = rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)
    assert v["estado"] == "resolvido" and v["faturas"][0]["link"] == "https://drive/a"
    p = {"tipo": "fatura_em_falta", "fornecedor": "Hostinger", "data": "2026-08-03", "valor": 17.99}
    assert rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)["estado"] == "com_aviso"
    p = {"tipo": "fatura_em_falta", "fornecedor": "Supabase", "data": "2026-08-17", "valor": 59.02}
    assert rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)["estado"] == "por_validar"
    p = {"tipo": "movimento_a_esclarecer", "fornecedor": "Poupança Omnai", "data": "2026-08-22", "valor": 1500.0}
    assert "interna" in rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)["nota"]
    p = {"tipo": "movimento_a_esclarecer", "fornecedor": "David Sardinha", "data": "2026-08-11", "valor": 691.2}
    v = rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)
    assert v["estado"] == "por_tratar" and "socio" in v["nota"]
    p = {"tipo": "fatura_em_falta", "fornecedor": "Indigo", "data": "2026-08-25", "valor": 70.0}
    v = rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)
    assert v["estado"] == "por_tratar" and v["movimentos"]
    p = {"tipo": "fatura_em_falta", "fornecedor": "Anthropic", "data": "2026-08-09", "valor": 38.25}
    assert rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)["estado"] == "so_recibo"
    p = {"tipo": "documento_exterior", "fornecedor": "Previnsa", "referencia": "M/3", "valor": 1230.0, "data": "2026-08-05"}
    assert "Moloni" in rc._verificar_um(p, FATURAS, MOVS, MOLONI, links)["nota"]


def test_extrair_e_texto(monkeypatch):
    import services.visao as visao
    chamadas = {}

    async def fake(system, prompt, imagens, max_tokens=2000, model=None):
        chamadas["imagens"] = len(imagens)
        return '```json\n{"pedidos": [{"tipo": "fatura_em_falta", "fornecedor": "MEO", "data": "26/07/2026", "valor": "92,02", "moeda": "EUR", "referencia": "FT A/871612871", "descricao": "fatura MEO"}, {"tipo": "extrato", "descricao": "extrato Julho"}], "periodo": "2026-07", "prazo": "2026-09-10", "resumo": "faturas em falta"}\n```'

    monkeypatch.setattr(visao, "generate_multimodal", fake)
    msg = {"from": "Eugénia Correia <geral@eugest.pt>", "subject": "Faturas", "date": "x",
           "corpo": "bom dia", "imagens": [(b"x" * 300, "image/png")], "pdfs": []}
    extr = asyncio.run(rc.extrair_pedidos(msg))
    assert chamadas["imagens"] == 1
    assert extr["pedidos"][0]["data"] == "2026-07-26" and extr["pedidos"][0]["valor"] == 92.02
    assert extr["pedidos"][1]["tipo"] == "extrato"
    for p in extr["pedidos"]:
        p["verificacao"] = rc._verificar_um(p, FATURAS, MOVS, MOLONI, {})
    txt = rc.texto_resposta(extr, extr["pedidos"], rc._nome_remetente(msg["from"]))
    assert txt.startswith("Olá Eugénia,")
    assert rc.texto_resposta(extr, extr["pedidos"], rc._nome_remetente("Geral Eugest <geral@eugest.pt>")).startswith("Bom dia,") and "[A TRATAR]" in txt and "1. 26/07 MEO FT A/871612871 92,02 EUR" in txt
    assert rc._endereco(msg["from"]) == "geral@eugest.pt"


def test_mcp_lists_new_tools():
    import mcp_server
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"fecho_pacote", "fecho_marcar_entregues", "resposta_contabilidade", "arquivo_verificar"} <= names
    assert set(mcp_server.HANDLERS) == names
