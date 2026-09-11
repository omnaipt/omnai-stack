"""Cliente Google Drive para arquivo de faturas.

Usa token OAuth da conta davidsardinhalves@gmail.com com scope drive.file
(o token_drive.json do projecto gmail-multi-mcp original).

Organizacao no Drive:
    Faturas OMNAI/
        OMNAI/2026-Q2/
        Sopato/2026-Q2/
        Previnsa/
        JMSoares/
        Pessoal/

As pastas sao criadas on-demand se nao existirem. O ID da pasta raiz e
cacheado em ficheiro para evitar chamadas repetidas.
"""
from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from typing import Optional

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

log = structlog.get_logger()

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
TOKENS_DIR = SECRETS_DIR / "tokens"
# 03-08-2026: conta OMNAI. O ficheiro pode ser trocado por ambiente
# sem tocar no codigo, que e o que faltava da ultima vez que o token
# expirou.
DRIVE_TOKEN_FILE = TOKENS_DIR / os.getenv(
    "DRIVE_TOKEN_FILE", "drive_david_sardinha_at_omnai_pt.json")

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

DRIVE_ROOT_NAME = os.getenv("DRIVE_ROOT_FOLDER", "Faturas OMNAI")
CACHE_FILE = Path(os.getenv("FATURAS_DIR", "/faturas")) / "_drive_folder_cache.json"


def _load_creds() -> Credentials:
    if not DRIVE_TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Token Drive nao encontrado em {DRIVE_TOKEN_FILE}. "
            f"Sobe o token_drive.json do projecto gmail-multi-mcp (renomeia para drive_davidsardinhalves.json)."
        )
    creds = Credentials.from_authorized_user_file(str(DRIVE_TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            DRIVE_TOKEN_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError("Token Drive invalido sem refresh.")
    return creds


def _service():
    return build("drive", "v3", credentials=_load_creds(), cache_discovery=False)


# ---- Cache de folder IDs ----

def _load_cache() -> dict[str, str]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as exc:
        log.warning("drive.cache_save_failed", err=str(exc))


# ---- Pastas ----

def _find_folder(svc, name: str, parent_id: Optional[str] = None) -> Optional[str]:
    """Procura pasta com o nome exacto, opcionalmente dentro de parent."""
    q = [
        f"mimeType='application/vnd.google-apps.folder'",
        f"name='{name.replace(chr(39), chr(92) + chr(39))}'",
        "trashed=false",
    ]
    if parent_id:
        q.append(f"'{parent_id}' in parents")
    query = " and ".join(q)
    try:
        r = svc.files().list(
            q=query, spaces="drive", fields="files(id,name,parents)",
            pageSize=10,
        ).execute()
        files = r.get("files", [])
        return files[0]["id"] if files else None
    except HttpError as exc:
        log.warning("drive.find_failed", name=name, err=str(exc))
        return None


def _create_folder(svc, name: str, parent_id: Optional[str] = None) -> str:
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = svc.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _ensure_folder(svc, name: str, parent_id: Optional[str], cache: dict[str, str]) -> str:
    cache_key = f"{parent_id or 'root'}::{name}"
    if cache_key in cache:
        return cache[cache_key]
    fid = _find_folder(svc, name, parent_id)
    if not fid:
        fid = _create_folder(svc, name, parent_id)
        log.info("drive.folder_created", name=name, parent=parent_id or "root", id=fid)
    cache[cache_key] = fid
    return fid


def ensure_invoice_folder(company: str, quarter: str, subpasta: str | None = None) -> str:
    """Garante que existe <ROOT>/<Company>/<Quarter>[/<subpasta>] no Drive e devolve o ID da ultima."""
    partes = [company, quarter] + ([subpasta] if subpasta else [])
    return ensure_path(partes)


def ensure_path(partes: list[str], svc=None) -> str:
    """Garante <ROOT>/<partes...> e devolve o ID da ultima pasta. 11-09-2026."""
    svc = svc or _service()
    cache = _load_cache()
    fid = _ensure_folder(svc, DRIVE_ROOT_NAME, None, cache)
    for nome in partes:
        fid = _ensure_folder(svc, nome, fid, cache)
    _save_cache(cache)
    return fid


def list_folder(folder_id: str, svc=None) -> list[dict]:
    """Ficheiros (nao pastas) numa pasta: id, name, mimeType, size, webViewLink."""
    svc = svc or _service()
    out, token = [], None
    while True:
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces="drive", pageSize=200, pageToken=token,
            fields="nextPageToken,files(id,name,mimeType,size,webViewLink,modifiedTime)",
        ).execute()
        out.extend(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            return out


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def upload_bytes_to(folder_id: str, data: bytes, filename: str,
                    mime_type: str = "application/pdf", svc=None) -> dict:
    """Upload para uma pasta conhecida, sem duplicar pelo nome."""
    svc = svc or _service()
    existente = svc.files().list(
        q=("'%s' in parents and name='%s' and trashed=false"
           % (folder_id, filename.replace("'", "\\'"))),
        spaces="drive", fields="files(id,name,webViewLink)",
    ).execute().get("files", [])
    if existente:
        return {**existente[0], "ja_existia": True}
    media = MediaIoBaseUpload(BytesIO(data), mimetype=mime_type, resumable=False)
    return svc.files().create(body={"name": filename, "parents": [folder_id]},
                              media_body=media, fields="id,name,webViewLink").execute()


def download_bytes(file_id: str, svc=None) -> bytes:
    svc = svc or _service()
    return svc.files().get_media(fileId=file_id).execute()


def upload_file(
    local_path: Path,
    filename: str,
    company: str,
    quarter: str,
    mime_type: str = "application/pdf",
) -> dict:
    """Faz upload de um ficheiro para <ROOT>/<Company>/<Quarter>/<filename>.

    Retorna dict com id, name, webViewLink.
    Se ja existir ficheiro com mesmo nome, nao duplica (retorna o existente).
    """
    svc = _service()
    target_folder = ensure_invoice_folder(company, quarter)

    # Verificar se ja existe ficheiro com este nome na pasta
    try:
        existing = svc.files().list(
            q=(
                f"'{target_folder}' in parents and name='"
                f"{filename.replace(chr(39), chr(92) + chr(39))}' and trashed=false"
            ),
            spaces="drive",
            fields="files(id,name,webViewLink)",
        ).execute()
        files = existing.get("files", [])
        if files:
            log.info("drive.upload_skipped_duplicate", name=filename, id=files[0]["id"])
            return files[0]
    except HttpError as exc:
        log.warning("drive.check_existing_failed", err=str(exc))

    media = MediaIoBaseUpload(
        BytesIO(Path(local_path).read_bytes()),
        mimetype=mime_type,
        resumable=False,
    )
    metadata = {"name": filename, "parents": [target_folder]}
    try:
        f = svc.files().create(
            body=metadata, media_body=media, fields="id,name,webViewLink",
        ).execute()
        log.info("drive.uploaded", name=filename, id=f.get("id"))
        return f
    except HttpError as exc:
        log.warning("drive.upload_failed", name=filename, err=str(exc))
        raise


def upload_bytes(
    pdf_bytes: bytes,
    filename: str,
    company: str,
    quarter: str,
    mime_type: str = "application/pdf",
    subpasta: str | None = None,
) -> dict:
    """Upload directo de bytes sem passar por ficheiro local."""
    svc = _service()
    target_folder = ensure_invoice_folder(company, quarter, subpasta)

    # Mesma verificacao que o upload_file ja fazia. Sem ela, reprocessar
    # um email punha a mesma fatura duas vezes no Drive.
    try:
        existente = svc.files().list(
            q=("'%s' in parents and name='%s' and trashed=false"
               % (target_folder, filename.replace("'", "\\'"))),
            spaces="drive", fields="files(id,name,webViewLink)",
        ).execute().get("files", [])
        if existente:
            log.info("drive.upload_bytes_ja_existia", name=filename)
            return existente[0]
    except HttpError as exc:
        log.warning("drive.check_existing_failed", err=str(exc))

    media = MediaIoBaseUpload(BytesIO(pdf_bytes), mimetype=mime_type, resumable=False)
    metadata = {"name": filename, "parents": [target_folder]}
    try:
        f = svc.files().create(
            body=metadata, media_body=media, fields="id,name,webViewLink",
        ).execute()
        return f
    except HttpError as exc:
        log.warning("drive.upload_bytes_failed", err=str(exc))
        raise


def test_connection() -> dict:
    try:
        svc = _service()
        about = svc.about().get(fields="user(emailAddress,displayName),storageQuota").execute()
        user = about.get("user", {})
        return {
            "ok": True,
            "email": user.get("emailAddress"),
            "name": user.get("displayName"),
            "storage_used_gb": round(
                int(about.get("storageQuota", {}).get("usage", 0)) / (1024**3), 2
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
