"""Cliente Moloni (API tradicional REST, plano Flex). 08-09-2026.

Config em /secrets/moloni.json:
  {"client_id": "...", "client_secret": "...", "username": "...", "password": "...",
   "company_id": 12345,
   "access_token": "...", "access_expires": 0, "refresh_token": "..."}   # cache

Docs (moloni.pt/dev/autenticacao): grant por password, access token 1 hora,
refresh token 14 dias. Guardamos username/password porque o refresh de 14
dias morre se o worker falhar duas semanas; com password o cliente recupera
sozinho. Chamadas: POST https://api.moloni.pt/v1/<controller>/<action>/?access_token=..&json=true
com corpo application/x-www-form-urlencoded.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
CONFIG_FILE = SECRETS_DIR / "moloni.json"
BASE = "https://api.moloni.pt/v1"
TIMEOUT = 30.0


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def configured() -> bool:
    try:
        cfg = load_config()
    except FileNotFoundError:
        return False
    return all(cfg.get(k) for k in ("client_id", "client_secret", "username", "password"))


def _grant(cfg: dict, params: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{BASE}/grant/", params={"client_id": cfg["client_id"],
                                             "client_secret": cfg["client_secret"], **params})
    if r.status_code >= 400:
        raise RuntimeError(f"moloni grant {r.status_code}: {r.text[:300]}")
    tok = r.json()
    if "access_token" not in tok:
        raise RuntimeError(f"moloni grant sem access_token: {tok}")
    return tok


def access_token() -> str:
    cfg = load_config()
    if cfg.get("access_token") and time.time() < float(cfg.get("access_expires", 0)):
        return cfg["access_token"]
    tok = None
    if cfg.get("refresh_token"):
        try:
            tok = _grant(cfg, {"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]})
        except Exception as exc:
            log.warning("moloni.refresh_failed", err=str(exc))
    if tok is None:
        tok = _grant(cfg, {"grant_type": "password", "username": cfg["username"],
                           "password": cfg["password"]})
    cfg["access_token"] = tok["access_token"]
    cfg["refresh_token"] = tok.get("refresh_token", cfg.get("refresh_token"))
    cfg["access_expires"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 60
    save_config(cfg)
    return cfg["access_token"]


def call(controller: str, action: str, data: dict | None = None) -> Any:
    # Com json=true a Moloni le o corpo como JSON, enviado com o content-type
    # de formulario (e assim que a documentacao e o wrapper python-moloni fazem).
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(f"{BASE}/{controller}/{action}/",
                   params={"access_token": access_token(), "json": "true"},
                   content=json.dumps(data or {}),
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        raise RuntimeError(f"moloni {controller}/{action} {r.status_code}: {r.text[:300]}")
    return r.json()


def company_id() -> int:
    cfg = load_config()
    if cfg.get("company_id"):
        return int(cfg["company_id"])
    comps = call("companies", "getAll")
    if not comps:
        raise RuntimeError("moloni: sem empresas associadas ao utilizador")
    cfg["company_id"] = int(comps[0]["company_id"])
    save_config(cfg)
    return cfg["company_id"]


def companies() -> list[dict]:
    return call("companies", "getAll")


def documents(mes: str | None = None, offset: int = 0, qty: int = 200) -> list[dict]:
    """Documentos emitidos (faturas, recibos, notas). mes 'YYYY-MM' filtra por data."""
    data: dict[str, Any] = {"company_id": company_id(), "offset": offset, "qty": qty}
    if mes:
        y, m = mes.split("-")
        from calendar import monthrange
        data["filter"] = [
            {"field": "date", "comparison": ">=", "value": f"{y}-{m}-01"},
            {"field": "date", "comparison": "<=",
             "value": f"{y}-{m}-{monthrange(int(y), int(m))[1]:02d}"},
        ]
    docs = call("documents", "getAll", data) or []
    if mes:
        # a API devolve tudo apesar do filtro; filtramos aqui pela data
        docs = [d for d in docs if str(d.get("date", "")).startswith(mes)]
    # Nomenclatura Moloni: net_value e o total COM impostos, gross_value e a
    # base tributavel. Traduzimos para o vocabulario da contabilidade.
    return [{
        "document_id": d.get("document_id"),
        "tipo": (d.get("document_type") or {}).get("saft_code") or d.get("document_type_id"),
        "numero": d.get("document_set_name", "") + "/" + str(d.get("number", "")),
        "data": d.get("date"),
        "cliente": (d.get("customer") or {}).get("name") or d.get("entity_name"),
        "nif": (d.get("customer") or {}).get("vat") or d.get("entity_vat"),
        "base": d.get("gross_value"),
        "total": d.get("net_value"),
        "estado": d.get("status"),
    } for d in (docs or [])]


def document_pdf_link(document_id: int) -> str | None:
    r = call("documents", "getPDFLink", {"company_id": company_id(), "document_id": document_id})
    return (r or {}).get("url")


def test_connection() -> dict:
    try:
        comps = companies()
        return {"ok": True, "companies": [
            {"company_id": c.get("company_id"), "name": c.get("name"), "vat": c.get("vat")}
            for c in comps]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
