"""Worker learn_from_sapo_trash v1.0 (28 Abr 2026).

Aprende com a pasta Lixo/Trash de cada conta IMAP. Le os emails apagados nos
ultimos N dias, extrai padroes de remetentes e dominios, e persiste em
/secrets/learned_rules.json. O classifier v0.8.2 le este ficheiro ao arranque
e usa as regras como camada determinista adicional.

Triggers:
- Schedule semanal: Domingo 19h (antes do arquivo-faturas das 20h)
- Manual via POST /tasks/run/learn-from-sapo-trash

Output:
- /secrets/learned_rules.json com:
    learned_senders: {email: count}    (>= MIN_OCCURRENCES_SENDER)
    learned_domains: {domain: count}   (>= MIN_OCCURRENCES_DOMAIN)
    learned_subject_keywords: {kw: count}
    metadata (window_days, updated_at, totals)

Conservadorismo: nunca aprende enderecos do David ou dominios das suas
proprias empresas (allowlist). Threshold por defeito 3 ocorrencias para
sender, 5 para dominio, evita falsos positivos por um email isolado.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
IMAP_CONFIG_FILE = SECRETS_DIR / "imap_accounts.json"
LEARNED_RULES_FILE = SECRETS_DIR / "learned_rules.json"

# Pasta destino (Lixo/Trash) por dominio. Espelha o do imap_client v0.6.0.
_DOMAIN_TRASH_FOLDER = {
    "sapo.pt": "Lixo",
    "hostinger.com": "Trash",
    "omnai.pt": "Trash",
}
_TRASH_FOLDER_FALLBACKS = ["Trash", "Lixo", "Deleted Items", "INBOX.Trash", "INBOX.Lixo"]

# Thresholds (parametrizaveis via env)
WINDOW_DAYS = int(os.getenv("LEARN_WINDOW_DAYS", "7"))
MIN_OCCURRENCES_SENDER = int(os.getenv("LEARN_MIN_SENDER", "3"))
MIN_OCCURRENCES_DOMAIN = int(os.getenv("LEARN_MIN_DOMAIN", "5"))
MIN_OCCURRENCES_SUBJECT_KW = int(os.getenv("LEARN_MIN_SUBJECT_KW", "5"))
CAP_PER_ACCOUNT = int(os.getenv("LEARN_CAP_PER_ACCOUNT", "300"))

# Allowlist: nunca aprender estes (mesmo se aparecerem em Lixo).
ALLOWLIST_SENDERS = {
    "david.sardinha@omnai.pt",
    "david.sardinha@previnsa.com",
    "david.sardinha@jmsoares.pt",
    "david.sardinha@sapo.pt",
    "davidsardinhalves@gmail.com",
    "hello@omnai.pt",
    "sopato.cascais@gmail.com",
    "opaidapetinga@gmail.com",
}
ALLOWLIST_DOMAINS = {
    "omnai.pt",
    "previnsa.com",
    "jmsoares.pt",
}

# Stop words PT/EN para subject extraction
_STOP_WORDS = {
    "para", "isto", "como", "este", "esta", "esse", "essa", "tudo", "muito",
    "ainda", "depois", "antes", "ontem", "hoje", "amanha", "amanhã",
    "your", "this", "that", "from", "with", "have", "more", "about", "they",
    "their", "thanks", "please", "regards", "today", "tomorrow", "yesterday",
}


def _decode_header_safe(raw: str | None) -> str:
    if not raw:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")
    out: list[str] = []
    try:
        for part, enc in decode_header(raw):
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(part)
        return "".join(out)
    except Exception:
        return str(raw)


def _extract_email_addr(from_str: str) -> str:
    """'Name <email@x.com>' -> 'email@x.com'. Em caso de falha, normaliza."""
    if not from_str:
        return ""
    m = re.search(r"<([^>]+)>", from_str)
    if m:
        return m.group(1).strip().lower()
    m = re.search(r"[\w\.\-\+]+@[\w\.\-]+", from_str)
    return m.group(0).lower() if m else from_str.lower().strip()


def _domain_of(addr: str) -> str:
    if "@" not in addr:
        return ""
    return addr.split("@", 1)[1].lower().strip()


def _load_imap_accounts() -> list[dict[str, Any]]:
    if not IMAP_CONFIG_FILE.exists():
        log.warning("learn.no_imap_config")
        return []
    try:
        raw = json.loads(IMAP_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("learn.config_parse_failed", err=str(exc))
        return []
    if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
        return raw["accounts"]
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [{**v, "email": k} for k, v in raw.items() if isinstance(v, dict)]
    return []


def _select_trash(conn: imaplib.IMAP4_SSL, account_email: str) -> str | None:
    """Selecciona pasta Lixo/Trash em readonly. Devolve o nome da pasta."""
    domain = _domain_of(account_email)
    candidates: list[str] = []
    if domain in _DOMAIN_TRASH_FOLDER:
        candidates.append(_DOMAIN_TRASH_FOLDER[domain])
        candidates.append(f"INBOX.{_DOMAIN_TRASH_FOLDER[domain]}")
    candidates.extend(_TRASH_FOLDER_FALLBACKS)

    for cand in candidates:
        try:
            typ, _ = conn.select(cand, readonly=True)
            if typ == "OK":
                return cand
        except Exception:
            continue
    return None


def _scan_account(cfg: dict[str, Any]) -> tuple[Counter, Counter, Counter, dict]:
    """Retorna (senders_counter, domains_counter, subjects_counter, stats)."""
    account = cfg.get("email", "")
    host = cfg.get("imap_server") or cfg.get("host")
    port = int(cfg.get("imap_port") or cfg.get("port") or 993)
    password = cfg.get("password")

    senders: Counter = Counter()
    domains: Counter = Counter()
    subjects: Counter = Counter()
    stats = {"account": account, "messages_seen": 0, "messages_processed": 0, "folder": None, "error": None}

    if not (account and host and password):
        stats["error"] = "config incompleta"
        return senders, domains, subjects, stats

    try:
        M = imaplib.IMAP4_SSL(host, port)
        M.login(account, password)
    except Exception as exc:
        stats["error"] = f"login_failed: {exc}"
        log.warning("learn.login_failed", account=account, err=str(exc))
        return senders, domains, subjects, stats

    try:
        folder = _select_trash(M, account)
        if not folder:
            stats["error"] = "trash_folder_not_found"
            log.warning("learn.no_trash", account=account)
            return senders, domains, subjects, stats
        stats["folder"] = folder

        since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'SINCE "{since}"')
        if typ != "OK" or not data:
            stats["error"] = "search_failed"
            return senders, domains, subjects, stats

        ids = data[0].split()[-CAP_PER_ACCOUNT:]
        stats["messages_seen"] = len(ids)

        for mid in ids:
            try:
                # BODY.PEEK[] para nao mexer no flag (na pasta Lixo nao importa
                # mas e boa pratica).
                typ, d = M.fetch(
                    mid,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-ID LIST-UNSUBSCRIBE)])"
                )
                if typ != "OK" or not d or not d[0]:
                    continue
                raw = d[0][1] if isinstance(d[0], tuple) else b""
                if not raw:
                    continue
                headers = email.message_from_bytes(raw)

                from_raw = _decode_header_safe(headers.get("From", ""))
                addr = _extract_email_addr(from_raw)
                if addr and addr not in ALLOWLIST_SENDERS:
                    dom = _domain_of(addr)
                    if dom and dom not in ALLOWLIST_DOMAINS:
                        senders[addr] += 1
                        domains[dom] += 1

                subj = _decode_header_safe(headers.get("Subject", "")).lower()
                if subj:
                    # palavras alfa de 4+ chars
                    words = re.findall(r"\b[a-zà-ÿ]{4,}\b", subj)
                    for w in words[:8]:
                        if w not in _STOP_WORDS:
                            subjects[w] += 1

                stats["messages_processed"] += 1
            except Exception as exc:
                log.warning("learn.fetch_failed", account=account, err=str(exc))
                continue
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass

    return senders, domains, subjects, stats


async def run(payload: dict | None = None) -> dict[str, Any]:
    """Entry point do worker. Compativel com pipeline omnai_agents.

    payload aceita override opcional de window_days.
    """
    payload = payload or {}
    window_override = payload.get("window_days")
    global WINDOW_DAYS
    if window_override:
        try:
            WINDOW_DAYS = int(window_override)
        except Exception:
            pass

    log.info(
        "learn.start",
        window_days=WINDOW_DAYS,
        min_sender=MIN_OCCURRENCES_SENDER,
        min_domain=MIN_OCCURRENCES_DOMAIN,
    )

    accounts = _load_imap_accounts()
    if not accounts:
        return {"status": "error", "error": "no_imap_accounts"}

    senders_total: Counter = Counter()
    domains_total: Counter = Counter()
    subjects_total: Counter = Counter()
    per_account: list[dict] = []

    for cfg in accounts:
        s, d, sub, stats = await asyncio.to_thread(_scan_account, cfg)
        senders_total += s
        domains_total += d
        subjects_total += sub
        per_account.append(stats)

    # Aplicar thresholds
    learned_senders = {
        s: c for s, c in senders_total.items() if c >= MIN_OCCURRENCES_SENDER
    }
    learned_domains = {
        d: c for d, c in domains_total.items() if c >= MIN_OCCURRENCES_DOMAIN
    }
    learned_subject_kw = {
        w: c for w, c in subjects_total.items() if c >= MIN_OCCURRENCES_SUBJECT_KW
    }

    # Carregar regras existentes para preservar historico
    historic_senders: dict[str, int] = {}
    historic_domains: dict[str, int] = {}
    if LEARNED_RULES_FILE.exists():
        try:
            existing = json.loads(LEARNED_RULES_FILE.read_text(encoding="utf-8"))
            historic_senders = existing.get("learned_senders", {}) or {}
            historic_domains = existing.get("learned_domains", {}) or {}
        except Exception:
            pass

    # Merge: soma com historico (decaimento natural via janela)
    merged_senders = dict(historic_senders)
    for s, c in learned_senders.items():
        merged_senders[s] = max(historic_senders.get(s, 0), c)
    merged_domains = dict(historic_domains)
    for d, c in learned_domains.items():
        merged_domains[d] = max(historic_domains.get(d, 0), c)

    out = {
        "version": "1.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "min_occurrences_sender": MIN_OCCURRENCES_SENDER,
        "min_occurrences_domain": MIN_OCCURRENCES_DOMAIN,
        "learned_senders": merged_senders,
        "learned_domains": merged_domains,
        "learned_subject_keywords": learned_subject_kw,
        "this_run": {
            "senders_observed": len(senders_total),
            "domains_observed": len(domains_total),
            "subjects_observed": len(subjects_total),
            "senders_passed_threshold": len(learned_senders),
            "domains_passed_threshold": len(learned_domains),
            "subjects_passed_threshold": len(learned_subject_kw),
        },
        "per_account": per_account,
    }

    try:
        LEARNED_RULES_FILE.write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        log.warning("learn.persist_failed", err=str(exc))
        return {"status": "error", "error": f"persist_failed: {exc}"}

    log.info(
        "learn.done",
        senders=len(merged_senders),
        domains=len(merged_domains),
        subject_kws=len(learned_subject_kw),
        accounts=len(accounts),
    )

    return {
        "status": "ok",
        "window_days": WINDOW_DAYS,
        "totals": {
            "learned_senders": len(merged_senders),
            "learned_domains": len(merged_domains),
            "learned_subject_keywords": len(learned_subject_kw),
        },
        "this_run": out["this_run"],
        "top_senders_this_run": [
            {"addr": s, "count": c} for s, c in senders_total.most_common(10)
        ],
        "top_domains_this_run": [
            {"domain": d, "count": c} for d, c in domains_total.most_common(10)
        ],
        "per_account": per_account,
        "rules_file": str(LEARNED_RULES_FILE),
    }
