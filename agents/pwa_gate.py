"""Portao de sessao da PWA. 08-09-2026.

Ate hoje a app (Hoje, Fazer, Faturas, Empresas e todos os /api/*) estava
aberta ao publico em agents.omnai.pt: qualquer pessoa lia a fila de emails, os
PDFs das faturas e os dados fiscais das empresas, e podia validar ou ignorar
faturas. Este modulo fecha isso com o minimo de atrito para um utilizador so:

  * uma frase secreta, pedida uma vez por dispositivo em /entrar;
  * cookie assinado (HMAC) com validade de 90 dias, renovado a cada visita;
  * isentos: /health, /actions/* (ja tem HMAC proprio), /api/telegram/*
    (webhook e endpoints com X-OMNAI-Token), /mcp* (token proprio),
    /manifest.json, /sw.js, /static/*, /favicon.ico (senao a PWA nao instala).

Ligado apenas quando existe /secrets/pwa_gate.json com
  {"frase": "<frase secreta>", "chave": "<hex aleatorio>"}
Sem o ficheiro, o modulo nao faz nada e a app fica como estava. Isto permite
fazer deploy sem trancar o telemovel do David a meio: ele liga quando quiser
criando o ficheiro. Para desligar, basta apagar o ficheiro e reiniciar.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
CONFIG_FILE = SECRETS_DIR / "pwa_gate.json"
COOKIE = "omnai_sessao"
VALIDADE_S = 90 * 24 * 3600
EXEMPT_PREFIXES = ("/health", "/actions/", "/api/telegram/", "/mcp",
                   "/static/", "/entrar", "/sair")
EXEMPT_EXACT = ("/manifest.json", "/sw.js", "/favicon.ico", "/instalar")


def _config() -> dict | None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not cfg.get("frase") or not cfg.get("chave"):
        return None
    return cfg


def _sign(chave: str, exp: int) -> str:
    mac = hmac.new(chave.encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _valid_cookie(chave: str, value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp_s, mac = value.split(".", 1)
    if not exp_s.isdigit():
        return False
    exp = int(exp_s)
    if exp < time.time():
        return False
    return hmac.compare_digest(_sign(chave, exp), value)


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIXES)


_FORM = """<!DOCTYPE html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OMNAI</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#f6f7f9;color:#12151a}
@media (prefers-color-scheme:dark){body{background:#0b0d10;color:#e8ecf1}}
form{background:#fff;border:1px solid #e4e7ec;border-radius:14px;padding:24px;width:min(360px,90vw)}
@media (prefers-color-scheme:dark){form{background:#161a20;border-color:#242a33}}
h1{font-size:20px;margin:0 0 12px}
input{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border:1px solid #cfd4dc;border-radius:10px;margin:8px 0 12px}
button{width:100%;font-size:16px;padding:12px;border:0;border-radius:10px;background:#2563eb;color:#fff}
.err{color:#dc2626;font-size:14px;margin-top:8px}
</style></head><body>
<form method="post" action="/entrar">
<h1>OMNAI</h1>
<label for="frase">Frase de acesso</label>
<input id="frase" name="frase" type="password" autocomplete="current-password" autofocus>
<input type="hidden" name="next" value="{next}">
<button type="submit">Entrar</button>
{erro}
</form></body></html>"""


def install(app: FastAPI) -> None:
    """Regista rotas /entrar e /sair e o middleware. Inofensivo sem config."""

    @app.get("/entrar", response_class=HTMLResponse)
    async def entrar_form(request: Request):
        nxt = request.query_params.get("next") or "/"
        if not nxt.startswith("/"):
            nxt = "/"
        return HTMLResponse(_FORM.replace("{next}", nxt).replace("{erro}", ""))

    @app.post("/entrar")
    async def entrar_submit(request: Request):
        cfg = _config()
        # parse manual: python-multipart nao esta na imagem e request.form() exige-o
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        frase = (form.get("frase") or "").strip()
        nxt = form.get("next") or "/"
        if not str(nxt).startswith("/"):
            nxt = "/"
        if cfg is None:
            return RedirectResponse(url=str(nxt), status_code=303)
        if not hmac.compare_digest(frase.encode(), str(cfg["frase"]).encode()):
            html = _FORM.replace("{next}", str(nxt)).replace(
                "{erro}", '<div class="err">Frase errada.</div>')
            return HTMLResponse(html, status_code=401)
        exp = int(time.time()) + VALIDADE_S
        resp = RedirectResponse(url=str(nxt), status_code=303)
        resp.set_cookie(COOKIE, _sign(cfg["chave"], exp), max_age=VALIDADE_S,
                        httponly=True, secure=True, samesite="lax", path="/")
        return resp

    @app.get("/sair")
    async def sair():
        resp = RedirectResponse(url="/entrar", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.middleware("http")
    async def _portao(request: Request, call_next):
        cfg = _config()
        path = request.url.path
        if cfg is None or _is_exempt(path):
            return await call_next(request)
        if _valid_cookie(cfg["chave"], request.cookies.get(COOKIE)):
            resposta = await call_next(request)
            # renova a validade em cada visita a uma pagina HTML
            if resposta.headers.get("content-type", "").startswith("text/html"):
                exp = int(time.time()) + VALIDADE_S
                resposta.set_cookie(COOKIE, _sign(cfg["chave"], exp), max_age=VALIDADE_S,
                                    httponly=True, secure=True, samesite="lax", path="/")
            return resposta
        if path.startswith("/api/"):
            return JSONResponse({"error": "sessao em falta", "entrar": "/entrar"},
                                status_code=401)
        return RedirectResponse(url=f"/entrar?next={path}", status_code=303)
