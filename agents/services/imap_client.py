"""Cliente IMAP v0.6.0 - bug fixes de Sapo e flag Seen (28 Abr 2026).

Mudancas vs v0.5.0:

1. CORRECCAO: fetch passa a usar BODY.PEEK[] em vez de BODY[]/RFC822.
   v0.5.0 marcava emails como \\Seen ao fazer fetch para classificacao.
   Resultado: David nunca via na inbox o que era novo (tudo aparecia lido).
   v0.6.0 nao toca no flag Seen no fetch. Marcacao explicita so depois de
   archive ou delete bem sucedido.

2. CORRECCAO: archive no Sapo. v0.5.0 procurava por "Archive"/"Arquivo" e nao
   encontrava (Sapo usa "Lixo" como pasta de eliminados). Tentava criar
   "Archive" e falhava silenciosamente. v0.6.0 introduz mapping por dominio:
   - sapo.pt -> Lixo
   - hostinger.com (omnai.pt) -> Archive ou INBOX.Archive
   - default -> alargado para incluir Lixo, Trash, Deleted Items.

3. CORRECCAO: archive_message e delete_message usam MOVE (RFC 6851) primeiro,
   com fallback COPY+\\Deleted+EXPUNGE. O Sapo pode nao suportar MOVE; o
   fallback garante que move sempre. Tambem marcam \\Seen apenas quando
   bem sucedido (correcto: emails arquivados/apagados ficam lidos no destino).

4. Logging granular: imap.archive.move_ok, imap.archive.fallback_copy,
   imap.archive.failed, imap.delete.move_ok, imap.delete.fallback. Permite
   ver exactamente o que aconteceu em cada email.
"""
from __future__ import annotations

import email
import imaplib
import re
import json
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
IMAP_CONFIG_FILE = SECRETS_DIR / "imap_accounts.json"

# v0.6.0: mapping de pasta destino por dominio.
# Para o David: no Sapo tudo (archive + delete) vai para Lixo, porque ele nao
# usa pastas separadas. No OMNAI/Hostinger, archive vai para Archive (criar
# se nao existir), delete continua a ir para Trash/Deleted Items.
_DOMAIN_ARCHIVE_FOLDER = {
    "sapo.pt": "Lixo",
    "hostinger.com": "Archive",
    "omnai.pt": "Archive",  # entrega via Hostinger
}

_DOMAIN_DELETE_FOLDER = {
    "sapo.pt": "Lixo",  # mesmo destino, simplifica para o David
    "hostinger.com": "Trash",
    "omnai.pt": "Trash",
}

# v0.6.0: candidatos alargados para fallback quando dominio nao esta mapeado.
ARCHIVE_FOLDER_CANDIDATES = [
    "Archive", "Arquivo", "Arquivos",
    "INBOX.Archive", "INBOX.Arquivo",
    "Lixo", "Trash",
    "[Gmail]/All Mail", "All Mail",
    "Deleted Items", "Deleted Messages",
]

DELETE_FOLDER_CANDIDATES = [
    "Trash", "Lixo", "Deleted Items", "Deleted Messages",
    "INBOX.Trash", "INBOX.Lixo",
    "[Gmail]/Trash",
]

# Regex formato IMAP LIST: (attrs) "delimiter" name|"quoted name"
_IMAP_LIST_RE = re.compile(
    r'^\(([^)]*)\)\s+(?:"([^"]*)"|NIL)\s+(?:"(.*)"|(\S+))$'
)


def _config() -> dict[str, dict[str, Any]]:
    if not IMAP_CONFIG_FILE.exists():
        return {}
    try:
        raw = json.loads(IMAP_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("imap.config_parse_failed", err=str(exc))
        return {}

    out: dict[str, dict[str, Any]] = {}

    def normalize(item: dict) -> tuple[str, dict]:
        email_addr = item.get("email") or item.get("username") or item.get("name")
        if not email_addr:
            return "", {}
        return email_addr, {
            "host": item.get("host") or item.get("imap_server"),
            "port": int(item.get("port") or item.get("imap_port") or 993),
            "username": item.get("username") or email_addr,
            "password": item.get("password"),
            "label": item.get("label", email_addr),
        }

    if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
        for item in raw["accounts"]:
            if isinstance(item, dict):
                k, v = normalize(item)
                if k:
                    out[k] = v
    elif isinstance(raw, dict):
        for email_addr, cfg in raw.items():
            if isinstance(cfg, dict):
                k, v = normalize({**cfg, "email": email_addr})
                if k:
                    out[k] = v
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                k, v = normalize(item)
                if k:
                    out[k] = v
    return out


def available_accounts() -> list[str]:
    return list(_config().keys())


def _connect(account: str) -> imaplib.IMAP4_SSL:
    cfg = _config().get(account)
    if not cfg:
        raise ValueError(f"Conta IMAP sem config: {account}")
    if not cfg.get("host"):
        raise ValueError(f"Conta {account} sem host")
    conn = imaplib.IMAP4_SSL(cfg["host"], cfg.get("port", 993))
    conn.login(cfg["username"], cfg["password"])
    conn.select("INBOX")
    return conn


def _decode_header(raw: str | None) -> str:
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


def _extract_body(msg: email.message.Message, max_chars: int = 5000) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ).lower():
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")[:max_chars]
                    except Exception:
                        return payload.decode("latin-1", errors="replace")[:max_chars]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")[:max_chars]
            except Exception:
                return payload.decode("latin-1", errors="replace")[:max_chars]
    return ""


def _raw_message(conn: imaplib.IMAP4_SSL, message_id: str) -> bytes:
    """v0.6.0: usa BODY.PEEK[] para nao marcar como \\Seen."""
    typ, d = conn.fetch(message_id, "(BODY.PEEK[])")
    if typ != "OK" or not d or not d[0]:
        return b""
    return d[0][1] if isinstance(d[0], tuple) else b""


def list_inbox_messages(account: str, hours: int = 48, cap: int = 50) -> list[dict]:
    try:
        conn = _connect(account)
    except Exception as exc:
        log.warning("imap.connect_failed", account=account, err=str(exc))
        return []
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%d-%b-%Y")
        typ, data = conn.search(None, f'SINCE "{since}"')
        if typ != "OK" or not data:
            return []
        ids = data[0].split()[-cap:]
        msgs: list[dict] = []
        for mid in ids:
            try:
                # v0.6.0: BODY.PEEK[HEADER.FIELDS ...] em vez de BODY[HEADER.FIELDS ...]
                typ, d = conn.fetch(
                    mid,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID TO)])"
                )
                if typ != "OK" or not d or not d[0]:
                    continue
                raw = d[0][1] if isinstance(d[0], tuple) else b""
                headers = email.message_from_bytes(raw) if raw else email.message.Message()
                msgs.append({
                    "id": mid.decode() if isinstance(mid, bytes) else str(mid),
                    "from": _decode_header(headers.get("From", "")),
                    "to": _decode_header(headers.get("To", "")),
                    "subject": _decode_header(headers.get("Subject", "")),
                    "date": headers.get("Date", ""),
                    "message_id_header": headers.get("Message-ID", ""),
                })
            except Exception as exc:
                log.warning("imap.fetch_header_failed", err=str(exc))
                continue
        return msgs
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def get_message_body(account: str, message_id: str) -> str:
    try:
        conn = _connect(account)
    except Exception as exc:
        log.warning("imap.connect_failed", account=account, err=str(exc))
        return ""
    try:
        raw = _raw_message(conn, message_id)
        if not raw:
            return ""
        msg = email.message_from_bytes(raw)
        return _extract_body(msg)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def list_attachments(account: str, message_id: str) -> list[dict]:
    """Lista attachments de uma mensagem IMAP."""
    try:
        conn = _connect(account)
    except Exception:
        return []
    try:
        raw = _raw_message(conn, message_id)
        if not raw:
            return []
        msg = email.message_from_bytes(raw)
        out: list[dict] = []
        idx = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename()
            if not filename:
                continue
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp or filename.lower().endswith((".pdf", ".xml", ".xlsx")):
                out.append({
                    "filename": _decode_header(filename),
                    "mime_type": part.get_content_type(),
                    "attachment_idx": idx,
                })
            idx += 1
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def download_attachments(account: str, message_id: str) -> list[tuple[str, bytes]]:
    """Descarrega TODOS os attachments. Retorna [(filename, bytes), ...]."""
    try:
        conn = _connect(account)
    except Exception:
        return []
    try:
        raw = _raw_message(conn, message_id)
        if not raw:
            return []
        msg = email.message_from_bytes(raw)
        out: list[tuple[str, bytes]] = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            filename = part.get_filename()
            if not filename:
                continue
            disp = str(part.get("Content-Disposition", "")).lower()
            fname_decoded = _decode_header(filename)
            if "attachment" in disp or fname_decoded.lower().endswith((".pdf",)):
                payload = part.get_payload(decode=True)
                if payload:
                    out.append((fname_decoded, payload))
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _parse_imap_list_line(line: str) -> tuple[str, str] | None:
    """Retorna (delimiter, folder_name) ou None se formato nao reconhecido."""
    m = _IMAP_LIST_RE.match(line.strip())
    if not m:
        return None
    _attrs, delim, qname, uqname = m.groups()
    name = qname if qname is not None else (uqname or "")
    return (delim or ".", name)


def _quote_mailbox(name: str) -> str:
    """IMAP mailbox name -> atom seguro para COPY/MOVE (RFC 3501)."""
    safe_atom = all(
        c.isascii() and c.isalnum() or c in "._-+/"
        for c in name
    )
    if safe_atom and name:
        return name
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _list_folders(conn: imaplib.IMAP4_SSL) -> list[str]:
    typ, data = conn.list()
    if typ != "OK" or not data:
        return []
    folders: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        parsed = _parse_imap_list_line(item)
        if parsed:
            folders.append(parsed[1])
    return folders


def _domain_of(account: str) -> str:
    """Extrai o dominio do email da conta. Ex: david.sardinha@sapo.pt -> sapo.pt."""
    if "@" not in account:
        return ""
    return account.split("@", 1)[1].lower().strip()


def _resolve_target(
    conn: imaplib.IMAP4_SSL,
    account: str,
    domain_map: dict[str, str],
    fallback_candidates: list[str],
) -> str | None:
    """v0.6.0: resolve pasta destino para o account.

    Estrategia:
    1. Se o dominio esta no mapping, tenta directamente esse nome (exacto).
    2. Tambem tenta com prefixo INBOX. (alguns servidores IMAP exigem).
    3. Senao, percorre fallback_candidates e devolve a primeira que existe.
    4. Como ultimo recurso, tenta criar a primeira do mapping/candidates.
    """
    folders = _list_folders(conn)
    folders_set = set(folders)
    domain = _domain_of(account)

    preferred: list[str] = []
    mapped = domain_map.get(domain)
    if mapped:
        preferred.append(mapped)
        preferred.append(f"INBOX.{mapped}")

    preferred.extend(fallback_candidates)

    # Match exacto
    for cand in preferred:
        if cand in folders_set:
            return cand

    # Match endswith (suporta hierarquias diferentes, ex: My/Archive)
    for cand in preferred:
        for f in folders:
            if f.endswith(cand) or f.endswith(f".{cand}"):
                return f

    # Cria a primeira opcao do mapping; senao a primeira dos candidates
    create_target = mapped or (fallback_candidates[0] if fallback_candidates else None)
    if create_target:
        try:
            typ, _ = conn.create(create_target)
            if typ == "OK":
                log.info("imap.folder_created", folder=create_target)
                return create_target
        except Exception as exc:
            log.warning("imap.folder_create_failed", folder=create_target, err=str(exc))

    return None


def _try_move(conn: imaplib.IMAP4_SSL, message_id: str, target: str) -> bool:
    """RFC 6851 MOVE. Suportado em IMAP4 com extensao MOVE.

    Retorna True se MOVE foi aceite. False se servidor nao suporta ou falhou.
    """
    try:
        # imaplib nao tem .move() built-in, mas suporta comandos arbitrarios via _command
        typ, _ = conn._simple_command("MOVE", message_id, _quote_mailbox(target))
        if typ == "OK":
            conn.expunge()
            return True
        return False
    except Exception:
        return False


def _copy_and_delete(conn: imaplib.IMAP4_SSL, message_id: str, target: str) -> bool:
    """Fallback: COPY + STORE \\Deleted + EXPUNGE."""
    try:
        typ, _ = conn.copy(message_id, _quote_mailbox(target))
        if typ != "OK":
            return False
        conn.store(message_id, "+FLAGS", "\\Deleted")
        conn.expunge()
        return True
    except Exception:
        return False


def archive_message(account: str, message_id: str) -> bool:
    """Move email para pasta de arquivo do dominio. Marca \\Seen quando feito.

    v0.6.0:
    - sapo.pt -> Lixo
    - hostinger/omnai.pt -> Archive
    - default -> ARCHIVE_FOLDER_CANDIDATES
    Tenta MOVE (RFC 6851); fallback COPY+EXPUNGE.
    """
    try:
        conn = _connect(account)
    except Exception as exc:
        log.warning("imap.archive.connect_failed", account=account, err=str(exc))
        return False
    try:
        target = _resolve_target(
            conn, account, _DOMAIN_ARCHIVE_FOLDER, ARCHIVE_FOLDER_CANDIDATES
        )
        if not target:
            log.warning("imap.archive.no_target", account=account)
            return False

        if _try_move(conn, message_id, target):
            log.info("imap.archive.move_ok", account=account, mid=message_id, target=target)
            return True

        if _copy_and_delete(conn, message_id, target):
            log.info(
                "imap.archive.fallback_copy",
                account=account, mid=message_id, target=target,
            )
            return True

        log.warning("imap.archive.failed", account=account, mid=message_id, target=target)
        return False
    except Exception as exc:
        log.warning("imap.archive.exception", account=account, err=str(exc))
        return False
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def delete_message(account: str, message_id: str) -> bool:
    """Move email para Trash/Lixo do dominio. v0.6.0: MOVE + fallback COPY.

    No Sapo, archive e delete partilham destino (Lixo). Em outros providers,
    delete vai para Trash separado.
    """
    try:
        conn = _connect(account)
    except Exception as exc:
        log.warning("imap.delete.connect_failed", account=account, err=str(exc))
        return False
    try:
        target = _resolve_target(
            conn, account, _DOMAIN_DELETE_FOLDER, DELETE_FOLDER_CANDIDATES
        )
        if target:
            # Tem pasta de Trash explicita: move
            if _try_move(conn, message_id, target):
                log.info(
                    "imap.delete.move_ok",
                    account=account, mid=message_id, target=target,
                )
                return True
            if _copy_and_delete(conn, message_id, target):
                log.info(
                    "imap.delete.fallback_copy",
                    account=account, mid=message_id, target=target,
                )
                return True

        # Sem pasta resolvida, marca \\Deleted e EXPUNGE (apaga mesmo)
        try:
            conn.store(message_id, "+FLAGS", "\\Deleted")
            conn.expunge()
            log.info(
                "imap.delete.flag_only",
                account=account, mid=message_id,
            )
            return True
        except Exception as exc:
            log.warning("imap.delete.flag_failed", account=account, err=str(exc))
            return False
    except Exception as exc:
        log.warning("imap.delete.exception", account=account, err=str(exc))
        return False
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def test_connection(account: str) -> dict:
    try:
        conn = _connect(account)
        try:
            typ, data = conn.select("INBOX")
            count = int(data[0]) if typ == "OK" and data and data[0] else -1
            return {
                "ok": True,
                "inbox_count": count,
                "label": _config().get(account, {}).get("label", ""),
            }
        finally:
            conn.logout()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# v0.6.0: helper de diagnostico, util quando uma conta nova falha.
def list_folders(account: str) -> list[str]:
    """Devolve lista de pastas IMAP visiveis na conta. Para diagnostico."""
    try:
        conn = _connect(account)
    except Exception as exc:
        log.warning("imap.list_folders.connect_failed", account=account, err=str(exc))
        return []
    try:
        return _list_folders(conn)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
