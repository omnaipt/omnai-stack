"""Utilitarios de texto partilhados.

`slugify` substitui a versao rudimentar que hoje vive em services/invoices.py
(apenas lower + regex). Aqui faz normalizacao NFKD para transliterar
acentos antes do replace, resolvendo o bug `guas-de-cascais` -> `aguas-de-cascais`.
"""
from __future__ import annotations

import re
import unicodedata


def slugify(text: str, *, max_len: int = 80, sep: str = "-") -> str:
    """Devolve slug seguro para nomes de ficheiro.

    Pipeline:
    1. NFKD -> transliterar acentos (a vez de a, c vez de c, etc.)
    2. ASCII-only (ignora caracteres impossiveis de converter)
    3. Lowercase
    4. Substituir tudo o que nao seja [a-z0-9] por sep
    5. Colapsar seps repetidos
    6. Strip sep das pontas
    7. Truncar a max_len preservando palavra

    >>> slugify("Aguas de Cascais / Fatura 2026")
    'aguas-de-cascais-fatura-2026'
    >>> slugify("Ana-Bo (T)")
    'ana-bo-t'
    >>> slugify("")
    'sem-nome'
    """
    if not text:
        return "sem-nome"

    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()

    # Substituir tudo o que nao e alfanumerico
    cleaned = re.sub(r"[^a-z0-9]+", sep, lowered)
    cleaned = re.sub(f"{re.escape(sep)}+", sep, cleaned)
    cleaned = cleaned.strip(sep)

    if not cleaned:
        return "sem-nome"

    if len(cleaned) <= max_len:
        return cleaned

    # Trim a max_len preservando limite de palavra quando possivel
    trimmed = cleaned[:max_len]
    last_sep = trimmed.rfind(sep)
    if last_sep > max_len - 20:
        trimmed = trimmed[:last_sep]
    return trimmed.rstrip(sep) or "sem-nome"


def truncate_subject(subject: str, max_len: int = 120) -> str:
    """Trim de assunto preservando codificacao, para logs e briefings."""
    if not subject:
        return "(sem assunto)"
    subject = subject.replace("\n", " ").replace("\r", " ").strip()
    if len(subject) <= max_len:
        return subject
    return subject[: max_len - 1].rstrip() + "..."
