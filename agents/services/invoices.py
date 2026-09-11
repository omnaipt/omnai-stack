"""Extraccao e arquivo organizado de faturas.

Fluxo:
1. Extrai texto do PDF com pypdf
2. Claude estrutura metadados (supplier, date, amount, invoice_number)
3. Guarda local em /faturas/<Empresa>/<Ano>-Q<N>/<slug>.pdf
4. Upload para Google Drive na mesma estrutura (se DRIVE_ENABLED)
5. Falha ao extrair -> escreve no manual queue
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import structlog
from pypdf import PdfReader

from services.llm import generate

log = structlog.get_logger()

FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
DRIVE_ENABLED = os.getenv("DRIVE_ENABLED", "true").lower() in ("1", "true", "yes")

INBOX_TO_COMPANY = {
    "hello@omnai.pt": "OMNAI",
    "david.sardinha@omnai.pt": "OMNAI",
    "davidsardinhalves@gmail.com": "OMNAI",
    "sopato.cascais@gmail.com": "Sopato",
    "opaidapetinga@gmail.com": "Previnsa",
    "david.sardinha@previnsa.com": "Previnsa",
    "david.sardinha@jmsoares.pt": "JMSoares",
    "david.sardinha@sapo.pt": "Pessoal",
}


METADATA_SYSTEM = """Extrai metadados de uma fatura/recibo a partir do texto de um PDF.

Output OBRIGATÓRIO: JSON puro (sem markdown):
{
  "supplier": "Anthropic",
  "supplier_nif": "US12345678" ou null,
  "date": "2026-04-15",
  "amount": 90.00,
  "currency": "EUR",
  "invoice_number": "2163-2732-2755" ou null,
  "description": "Plano Max mensal" ou null
}

Regras:
- supplier: nome curto (sem "Ltd", "Inc", "PBC" se longo)
- date: formato ISO YYYY-MM-DD (data de emissão)
- amount: número (sem símbolo)
- currency: EUR, USD, GBP, etc.
- Campos desconhecidos: null
- Se não parecer fatura: todos null excepto supplier se nome claro"""


def _slugify(text: str, max_len: int = 40) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len] or "unknown"


def quarter_from_date(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


async def extract_metadata(
    pdf_bytes: bytes, hint_from: str = "", hint_subject: str = ""
) -> dict[str, Any]:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        text_parts: list[str] = []
        for page in reader.pages[:5]:
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(text_parts)[:8000]
    except Exception as exc:
        log.warning("pdf.read_failed", err=str(exc))
        return {"supplier": None, "date": None, "amount": None, "error": str(exc)}

    if not text.strip():
        return {
            "supplier": None, "date": None, "amount": None,
            "error": "PDF sem texto extractable",
        }

    prompt = (
        f"Remetente: {hint_from}\nAssunto: {hint_subject}\n\n"
        f"Texto do PDF (truncado):\n{text[:6000]}\n\nExtrai metadados."
    )
    try:
        resp = await generate(system=METADATA_SYSTEM, prompt=prompt, max_tokens=400)
    except Exception as exc:
        return {"supplier": None, "date": None, "amount": None, "error": f"llm: {exc}"}

    s = resp.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            s = "\n".join(lines).strip()
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        s = s[a : b + 1]
    try:
        return json.loads(s)
    except Exception as exc:
        log.warning("metadata.parse_failed", err=str(exc), raw=resp[:200])
        return {"supplier": None, "date": None, "amount": None, "error": "parse fail"}


def company_for_inbox(inbox: str) -> str:
    return INBOX_TO_COMPANY.get(inbox, "Outros")


HASHES_FILE = FATURAS_ROOT / "_hashes.json"
RECIBOS_SUB = "_recibos"


def _hashes() -> dict[str, str]:
    try:
        return json.loads(HASHES_FILE.read_text())
    except Exception:
        return {}


def _registar_hash(h: str, rel: str) -> None:
    try:
        d = _hashes()
        d[h] = rel
        HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        HASHES_FILE.write_text(json.dumps(d, indent=1))
    except Exception as exc:
        log.warning("hashes.save_failed", err=str(exc))


def reconstruir_hashes() -> int:
    """Percorre o arquivo e regista o sha256 de cada PDF. Idempotente."""
    d = _hashes()
    n = 0
    for pdf in FATURAS_ROOT.rglob("*.pdf"):
        rel = str(pdf.relative_to(FATURAS_ROOT))
        if rel.startswith("_tmp"):
            continue
        h = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if h not in d:
            d[h] = rel
            n += 1
    HASHES_FILE.write_text(json.dumps(d, indent=1))
    return n


def save_invoice_pdf(
    pdf_bytes: bytes,
    metadata: dict[str, Any],
    inbox: str,
) -> dict[str, Any]:
    company = company_for_inbox(inbox)

    # 11-09-2026: o mesmo PDF reenviado (ou o mesmo email lido duas vezes)
    # ficava como "<nome>_1.pdf" e ia em duplicado para a contabilidade.
    # Conteudo igual, byte a byte, nao se arquiva duas vezes.
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    ja = _hashes().get(sha)
    if ja and (FATURAS_ROOT / ja).exists():
        log.info("invoice.duplicado_por_conteudo", existente=ja)
        return {
            "company": company, "quarter": None, "path": str(FATURAS_ROOT / ja),
            "filename": Path(ja).name, "supplier": metadata.get("supplier"),
            "date": metadata.get("date"), "amount": metadata.get("amount"),
            "currency": metadata.get("currency", "EUR"),
            "invoice_number": metadata.get("invoice_number"),
            "size_bytes": len(pdf_bytes), "drive_url": None,
            "duplicado_de": ja, "sha256": sha,
        }

    # Recibo de pagamento: prova que se pagou, nao prova a despesa. Fica ao
    # lado, em _recibos, fora do pacote para a contabilidade.
    from services.doc_fiscal import e_recibo, verificar_destinatario
    texto = texto_pdf(pdf_bytes)
    recibo = e_recibo(texto)["e_recibo"]
    aviso = None if recibo else verificar_destinatario(texto, company)

    date_str = metadata.get("date")
    if date_str:
        try:
            invoice_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            invoice_date = date.today()
    else:
        invoice_date = date.today()

    quarter = quarter_from_date(invoice_date)

    supplier = metadata.get("supplier") or "unknown"
    amount = metadata.get("amount")
    currency = metadata.get("currency", "EUR")
    invoice_num = metadata.get("invoice_number")

    name_parts = [_slugify(supplier), invoice_date.isoformat()]
    if amount is not None:
        try:
            amt_str = f"{float(amount):.2f}".replace(".", "-")
            name_parts.append(f"{amt_str}{currency}")
        except Exception:
            pass
    if invoice_num:
        name_parts.append(_slugify(str(invoice_num), 20))
    filename = "_".join(name_parts) + ".pdf"

    if recibo:
        filename = filename[:-4] + "_recibo.pdf"
    target_dir = FATURAS_ROOT / company / quarter / (RECIBOS_SUB if recibo else "")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    if target_path.exists():
        i = 1
        while True:
            alt = target_dir / f"{target_path.stem}_{i}.pdf"
            if not alt.exists():
                target_path = alt
                filename = alt.name
                break
            i += 1

    target_path.write_bytes(pdf_bytes)
    _registar_hash(sha, str(target_path.relative_to(FATURAS_ROOT)))
    log.info("invoice.saved_local", company=company, quarter=quarter,
             path=str(target_path), recibo=recibo, aviso=aviso)

    result = {
        "company": company,
        "quarter": quarter,
        "path": str(target_path),
        "filename": filename,
        "supplier": supplier,
        "date": invoice_date.isoformat(),
        "amount": amount,
        "currency": currency,
        "invoice_number": invoice_num,
        "size_bytes": len(pdf_bytes),
        "drive_url": None,
        "recibo": recibo,
        "aviso": aviso,
        "sha256": sha,
    }

    # Upload para Google Drive (davidsardinhalves)
    if DRIVE_ENABLED:
        try:
            # Import tardio para nao partir se token Drive nao existir
            from services import drive
            info = drive.upload_bytes(pdf_bytes, filename, company, quarter,
                                      subpasta=RECIBOS_SUB if recibo else None)
            result["drive_url"] = info.get("webViewLink")
            result["drive_id"] = info.get("id")
            log.info("invoice.saved_drive", drive_id=info.get("id"))
        except FileNotFoundError as exc:
            log.warning("drive.token_missing", err=str(exc))
            result["drive_error"] = "token em falta"
        except Exception as exc:
            log.warning("drive.upload_failed", err=str(exc))
            result["drive_error"] = f"{type(exc).__name__}: {exc}"

    return result


def texto_pdf(pdf_bytes: bytes, paginas: int = 4) -> str:
    """Texto das primeiras paginas, para classificar antes de arquivar."""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:paginas])
    except Exception as exc:
        log.warning("pdf.texto_falhou", err=str(exc))
        return ""


def save_documento_triagem(
    pdf_bytes: bytes, metadata: dict, inbox: str, tipo: str, natureza: str = "",
) -> dict:
    """Guarda um documento que NAO e factura, fora do arquivo contabilistico.

    Nao sobe para o Drive de proposito: o que vai para a contabilidade tem de
    ser so despesa documentada. Fica em disco, com o motivo no caminho, para
    o David poder ver e discordar.
    """
    company = company_for_inbox(inbox)
    date_str = metadata.get("date")
    try:
        invoice_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        invoice_date = date.today()
    quarter = quarter_from_date(invoice_date)

    nome = "_".join([_slugify(metadata.get("supplier") or "documento"),
                     invoice_date.isoformat()]) + ".pdf"
    sub = "_entradas" if natureza == "entrada" else os.path.join("_nao_faturas", tipo)
    destino = FATURAS_ROOT / sub / company / quarter
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / nome
    i = 1
    while caminho.exists():
        caminho = destino / ("%s_%d.pdf" % (nome[:-4], i))
        i += 1
    caminho.write_bytes(pdf_bytes)
    log.info("documento.triagem", tipo=tipo, company=company, path=str(caminho))
    return {"company": company, "quarter": quarter, "path": str(caminho),
            "filename": caminho.name, "tipo": tipo, "natureza": natureza}
