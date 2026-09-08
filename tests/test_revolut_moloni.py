"""Testes de revolut (JWT + paginacao), movimentos_db (categorizacao + matcher) e moloni."""
import base64
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(ROOT))
TMP = Path("/tmp/mcp_test_secrets2")
TMP.mkdir(exist_ok=True)
os.environ["SECRETS_DIR"] = str(TMP)
os.environ.setdefault("DATABASE_URL", "postgresql://x")  # nunca usado nos testes

from services import movimentos_db, revolut, moloni  # noqa: E402


# ---------------------------------------------------------------- JWT

def _keypair():
    import rsa
    pub, priv = rsa.newkeys(1024)
    (TMP / "revolut_private.pem").write_bytes(priv.save_pkcs1())
    return pub


def test_client_assertion_rs256():
    import rsa
    pub = _keypair()
    cfg = {"iss": "agents.omnai.pt", "client_id": "cid", "private_key_file": "revolut_private.pem"}
    tok = revolut.client_assertion(cfg)
    h, p, s = tok.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    assert json.loads(base64.urlsafe_b64decode(pad(h))) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(base64.urlsafe_b64decode(pad(p)))
    assert payload["aud"] == "https://revolut.com" and payload["sub"] == "cid"
    assert payload["exp"] > time.time()
    assert rsa.verify(f"{h}.{p}".encode(), base64.urlsafe_b64decode(pad(s)), pub) == "SHA-256"


def test_pkcs8_rejected():
    (TMP / "k8.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n")
    with pytest.raises(RuntimeError):
        revolut._private_key({"private_key_file": "k8.pem"})


# ----------------------------------------------------- paginacao Revolut

def test_list_transactions_paginates(monkeypatch):
    calls = []
    page1 = [{"id": f"t{i}", "created_at": f"2026-08-{30 - i:02d}T10:00:00Z"} for i in range(3)]
    page2 = [{"id": "t2", "created_at": "2026-08-28T10:00:00Z"},
             {"id": "t9", "created_at": "2026-08-01T10:00:00Z"}]

    def fake_get(path, params=None):
        calls.append(params)
        return page1 if len(calls) == 1 else page2

    monkeypatch.setattr(revolut, "_get", fake_get)
    out = revolut.list_transactions(datetime(2026, 7, 1, tzinfo=timezone.utc), page=3)
    assert [t["id"] for t in out] == ["t0", "t1", "t2", "t9"]
    assert calls[1]["to"].startswith("2026-08-28")


# ------------------------------------------------------- categorizacao

CONTAS = {"acc-main": "Main", "acc-poup": "Poupança Omnai"}


def _tx(tipo, amount, **kw):
    leg = {"leg_id": kw.get("leg_id", "l1"), "account_id": kw.get("acc", "acc-main"),
           "amount": amount, "currency": kw.get("cur", "EUR"),
           "description": kw.get("desc", "")}
    if "cp" in kw:
        leg["counterparty"] = kw["cp"]
    if "bill" in kw:
        leg["bill_amount"], leg["bill_currency"] = kw["bill"]
    tx = {"id": kw.get("id", "tx1"), "type": tipo, "state": "completed",
          "created_at": "2026-07-17T09:00:00Z", "completed_at": "2026-07-17T09:00:01Z",
          "legs": [leg] + kw.get("legs2", []), "reference": kw.get("ref")}
    if "merchant" in kw:
        tx["merchant"] = {"name": kw["merchant"]}
    return tx


def test_categorias():
    interno = _tx("transfer", -1500, desc="Main · EUR -> Poupança Omnai · EUR",
                  legs2=[{"leg_id": "l2", "account_id": "acc-poup", "amount": 1500, "currency": "EUR"}])
    linhas = movimentos_db.legs_para_linhas(interno, CONTAS)
    assert [l["categoria"] for l in linhas] == ["interno", "interno"]
    assert linhas[0]["match_estado"] == "nao_precisa"

    card = _tx("card_payment", -53.72, merchant="Supabase", bill=(53.72, "USD"))
    l = movimentos_db.legs_para_linhas(card, CONTAS)[0]
    assert l["categoria"] == "despesa" and l["match_estado"] == "por_casar"
    assert l["valor_orig"] == Decimal("53.72") and l["moeda_orig"] == "USD"
    assert l["contraparte"] == "Supabase" and l["data"] == date(2026, 7, 17)

    assert movimentos_db.legs_para_linhas(_tx("fee", -0.5), CONTAS)[0]["categoria"] == "taxa"
    assert movimentos_db.legs_para_linhas(_tx("interest", 0.21), CONTAS)[0]["categoria"] == "juros"
    socio = movimentos_db.legs_para_linhas(_tx("transfer", -691.2, cp={"name": "DAVID SOARES SARDINHA ALVES"}), CONTAS)[0]
    assert socio["categoria"] == "despesa" and "socio" in socio["nota"]
    assert movimentos_db.legs_para_linhas(_tx("exchange", -10), CONTAS)[0]["categoria"] == "cambio"
    assert movimentos_db.legs_para_linhas(_tx("transfer", 900, cp={"name": "Cliente"}), CONTAS)[0]["categoria"] == "receita"
    ext = _tx("transfer", -184.5, cp={"name": "Correia Goncalves"})
    assert movimentos_db.legs_para_linhas(ext, CONTAS)[0]["categoria"] == "despesa"


# ------------------------------------------------------------- matcher

def test_pontuar():
    mov = {"valor": Decimal("-49.20"), "moeda": "EUR", "valor_orig": Decimal("53.72"),
           "moeda_orig": "USD", "contraparte": "Supabase", "descricao": "Supabase",
           "data": date(2026, 7, 18)}
    fat_ok = {"valor": Decimal("53.72"), "moeda": "USD", "fornecedor": "supabase",
              "data_fatura": date(2026, 7, 17)}
    fat_eur = {"valor": Decimal("49.20"), "moeda": "EUR", "fornecedor": "outro",
               "data_fatura": date(2026, 7, 17)}
    fat_longe = {"valor": Decimal("53.72"), "moeda": "USD", "fornecedor": "supabase",
                 "data_fatura": date(2026, 6, 1)}
    fat_nao = {"valor": Decimal("18.00"), "moeda": "EUR", "fornecedor": "anthropic",
               "data_fatura": date(2026, 7, 17)}
    assert movimentos_db.pontuar(mov, fat_ok) > movimentos_db.pontuar(mov, fat_eur) > 0
    assert movimentos_db.pontuar(mov, fat_longe) == 0
    assert movimentos_db.pontuar(mov, fat_nao) == 0
    # sem bill_amount: Resend 18.29 EUR vs fatura 20 USD, mesmo fornecedor -> aproximado
    resend = {"valor": Decimal("-18.29"), "moeda": "EUR", "valor_orig": None, "moeda_orig": None,
              "contraparte": "Resend", "descricao": "Resend", "data": date(2026, 7, 28), "tipo": "card_payment"}
    f_resend = {"valor": Decimal("20.00"), "moeda": "USD", "fornecedor": "resend", "data_fatura": date(2026, 7, 27)}
    f_outro = {"valor": Decimal("20.00"), "moeda": "USD", "fornecedor": "vercel", "data_fatura": date(2026, 7, 27)}
    assert movimentos_db.pontuar(resend, f_resend) > 1.0
    assert movimentos_db.pontuar(resend, f_outro) == 0
    # transferencia paga 19 dias depois da fatura: janela de 45 dias
    meo = {"valor": Decimal("-92.02"), "moeda": "EUR", "valor_orig": None, "moeda_orig": None,
           "contraparte": None, "descricao": "Meo, Sa", "data": date(2026, 8, 14), "tipo": "transfer"}
    f_meo = {"valor": Decimal("92.02"), "moeda": "EUR", "fornecedor": "meo", "data_fatura": date(2026, 7, 26)}
    assert movimentos_db.pontuar(meo, f_meo) > 2.0
    assert movimentos_db.pontuar({**meo, "tipo": "card_payment"}, f_meo) == 0


# --------------------------------------------------------------- moloni

def test_moloni_token_and_documents(monkeypatch):
    (TMP / "moloni.json").write_text(json.dumps({
        "client_id": "c", "client_secret": "s", "username": "u", "password": "p"}))
    assert moloni.configured()
    reqs = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if "/grant/" in str(request.url):
            assert request.url.params["grant_type"] == "password"
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600,
                                             "refresh_token": "RT"})
        if "/companies/getAll/" in str(request.url):
            return httpx.Response(200, json=[{"company_id": 77, "name": "OMNAI"}])
        if "/documents/getAll/" in str(request.url):
            body = json.loads(request.content.decode())
            assert body["company_id"] == 77 and body["filter"][0]["value"] == "2026-07-01"
            assert request.url.params["access_token"] == "AT" and request.url.params["json"] == "true"
            return httpx.Response(200, json=[{"document_id": 5, "number": 3, "document_set_name": "M",
                                              "date": "2026-07-20", "net_value": 100, "gross_value": 123,
                                              "customer": {"name": "Cliente X", "vat": "500"}}])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    docs = moloni.documents("2026-07")
    assert docs == [{"document_id": 5, "tipo": None, "numero": "M/3", "data": "2026-07-20",
                     "cliente": "Cliente X", "nif": "500", "base": 123, "total": 100, "estado": None}]
    cfg = moloni.load_config()
    assert cfg["access_token"] == "AT" and cfg["company_id"] == 77
    # segunda chamada reutiliza o token em cache: sem novo grant
    n_grants = sum("/grant/" in str(r.url) for r in reqs)
    moloni.documents("2026-07")
    assert sum("/grant/" in str(r.url) for r in reqs) == n_grants
