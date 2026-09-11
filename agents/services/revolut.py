"""Cliente Revolut Business API. 08-09-2026.

Config em /secrets/revolut.json:
  {
    "client_id": "...",            # ClientId mostrado pelo Revolut ao carregar o certificado
    "iss": "agents.omnai.pt",      # dominio do redirect URI registado
    "private_key_file": "revolut_private.pem",  # PKCS#1 (BEGIN RSA PRIVATE KEY), relativo a /secrets
    "env": "prod",                 # ou "sandbox"
    "refresh_token": "...",        # preenchido por exchange_code()
    "access_token": "...", "access_expires": 0   # cache, gerido aqui
  }

Fluxo (docs Revolut): certificado X.509 auto-assinado carregado na conta,
JWT RS256 (iss=dominio, sub=client_id, aud=https://revolut.com) como
client_assertion, consentimento OAuth uma vez (code valido 2 min), depois
refresh_token sem expiracao. Access token dura 40 min.

RS256 com a lib `rsa` (ja na imagem, dependencia do google-auth): assinatura
PKCS#1 v1.5 com SHA-256, que e exactamente o RS256.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
CONFIG_FILE = SECRETS_DIR / "revolut.json"
BASES = {
    "prod": "https://b2b.revolut.com/api/1.0",
    "sandbox": "https://sandbox-b2b.revolut.com/api/1.0",
}
TIMEOUT = 30.0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


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
    return bool(cfg.get("client_id") and cfg.get("refresh_token"))


def _base(cfg: dict) -> str:
    return BASES[cfg.get("env", "prod")]


def _private_key(cfg: dict):
    import rsa
    pem = (SECRETS_DIR / cfg.get("private_key_file", "revolut_private.pem")).read_bytes()
    if b"BEGIN PRIVATE KEY" in pem:
        raise RuntimeError(
            "chave em PKCS#8; converter: openssl rsa -in k.pem -traditional -out k1.pem")
    return rsa.PrivateKey.load_pkcs1(pem)


def client_assertion(cfg: dict, ttl_s: int = 300) -> str:
    """JWT RS256 assinado com a chave privada do certificado carregado."""
    import rsa
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iss": cfg["iss"], "sub": cfg["client_id"],
               "aud": "https://revolut.com", "exp": int(time.time()) + ttl_s}
    signing = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
              f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = rsa.sign(signing.encode("ascii"), _private_key(cfg), "SHA-256")
    return f"{signing}.{_b64url(sig)}"


def _token_request(cfg: dict, data: dict) -> dict:
    data = {**data, "client_id": cfg["client_id"],
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion(cfg)}
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(f"{_base(cfg)}/auth/token", data=data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        raise RuntimeError(f"revolut auth {r.status_code}: {r.text[:300]}")
    return r.json()


def exchange_code(code: str) -> dict:
    """Troca o code do consentimento (valido 2 min) por tokens e guarda-os."""
    cfg = load_config()
    tok = _token_request(cfg, {"grant_type": "authorization_code", "code": code})
    cfg["refresh_token"] = tok["refresh_token"]
    cfg["access_token"] = tok["access_token"]
    cfg["access_expires"] = int(time.time()) + int(tok.get("expires_in", 2400)) - 60
    save_config(cfg)
    return {"ok": True, "expires_in": tok.get("expires_in")}


def access_token() -> str:
    cfg = load_config()
    if cfg.get("access_token") and time.time() < float(cfg.get("access_expires", 0)):
        return cfg["access_token"]
    if not cfg.get("refresh_token"):
        raise RuntimeError("revolut sem refresh_token; correr o consentimento (scripts/revolut_setup.sh)")
    tok = _token_request(cfg, {"grant_type": "refresh_token",
                               "refresh_token": cfg["refresh_token"]})
    cfg["access_token"] = tok["access_token"]
    cfg["access_expires"] = int(time.time()) + int(tok.get("expires_in", 2400)) - 60
    if tok.get("refresh_token"):
        cfg["refresh_token"] = tok["refresh_token"]
    save_config(cfg)
    return cfg["access_token"]


def _get(path: str, params: dict | None = None) -> Any:
    cfg = load_config()
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{_base(cfg)}{path}", params=params or {},
                  headers={"Authorization": f"Bearer {access_token()}"})
    if r.status_code >= 400:
        raise RuntimeError(f"revolut {path} {r.status_code}: {r.text[:300]}")
    return r.json()


def list_accounts() -> list[dict]:
    return _get("/accounts")


def list_transactions(from_dt: datetime, to_dt: datetime | None = None,
                      account_id: str | None = None, page: int = 1000) -> list[dict]:
    """Todas as transaccoes entre from_dt e to_dt, paginando por created_at
    (ordem decrescente; o `to` da pagina seguinte e o created_at do ultimo)."""
    to_dt = to_dt or datetime.now(timezone.utc)
    out: list[dict] = []
    seen: set[str] = set()
    cursor = to_dt
    while True:
        params = {"from": from_dt.isoformat(), "to": cursor.isoformat(), "count": page}
        if account_id:
            params["account"] = account_id
        batch = _get("/transactions", params)
        novos = [t for t in batch if t.get("id") not in seen]
        for t in novos:
            seen.add(t["id"])
        out.extend(novos)
        if len(batch) < page or not novos:
            break
        last = batch[-1].get("created_at")
        if not last:
            break
        cursor = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return out


def test_connection() -> dict:
    try:
        accs = list_accounts()
        return {"ok": True, "accounts": [
            {"id": a.get("id"), "name": a.get("name"), "currency": a.get("currency"),
             "balance": a.get("balance"), "state": a.get("state")} for a in accs]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
