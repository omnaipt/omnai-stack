"""Classificador v0.8.2 - regras aprendidas dinamicas (28 Abr 2026).

Mudancas vs v0.8.0:

1. NOVO: leitura de /secrets/learned_rules.json no arranque do modulo.
   Senders e dominios que o David apagou repetidamente (>= 3x sender,
   >= 5x dominio, em janela de 7 dias) viram regra `delete` automatica.
   Worker `learn_from_sapo_trash` actualiza este ficheiro semanalmente.

2. Nova regra 4.5 (LEARNED): se from bate em learned_senders ou
   learned_domains, classifica como `delete` com reason
   `heuristic.learned_from_trash`. Corre antes da regra 5
   (bulk_sender_default) para que padroes aprendidos prevalecam.

3. Funcao publica `reload_learned_rules()` para hot-reload sem restart
   (chamada no fim do worker learn_from_sapo_trash).

Mantem todo o resto da v0.8.0: bulk sender patterns, anti-fatura, etc.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from services.llm import generate

log = structlog.get_logger()

SYSTEM_PROMPT = """És um classificador de emails da OMNAI (David Sardinha, CEO de OMNAI, Previnsa, JMSoares, Sopato).

Classifica cada email em UMA de 5 classes:

1. "actionable" — REQUER resposta ou decisão do David (pessoa humana com pedido claro).
   Exemplos: cliente a pedir orçamento, colega a pedir aprovação, convite para reunião FUTURA.

2. "invoice" — Email contém fatura, recibo ou aviso de pagamento que precisa de ser arquivado como PDF.
   Exemplos:
   * Recibos/faturas de SaaS (Anthropic, Supabase, OpenAI, Google, Apple, etc.)
   * Faturas de utilities (Águas de Cascais, MEO, EDP, gás, etc.)
   * Invoices/payment confirmations with attachment
   * Confirmações de pagamento com PDF anexo
   * Mesmo que não tenha PDF anexo, se for claramente um recibo/fatura
   → Vai ser tratado pelo pipeline de arquivo automático de faturas

3. "archive" — Informativo sem ser fatura, sem acção. Pode sair da inbox.
   APENAS arquivar se cair num destes grupos, e só quando NÃO estás em dúvida:
   * Newsletters (TAAFT, Artlist, Netflix, AEP, Opto, Substack, Medium)
   * Redes sociais (LinkedIn notifications, Facebook, X/Twitter, Instagram, Threads, YouTube, TikTok)
   * Notificações de plataforma puramente informativas (GitHub weekly digest, Vercel deployment success)
   * Emails de "Termos e Condições" (excepto contratos reais)
   * Confirmações automáticas simples (Revolut T&C, Massimo Dutti e-ticket)
   * Conversas de reuniões que JÁ aconteceram (data no passado)

4. "delete" — Ruído, spam, cold outreach, alertas automáticos que não precisam de registo.
   IMPORTANTE: Estas são regras específicas dadas pelo David. Aplica-as sempre:
   * Alertas de segurança Google ("no-reply@accounts.google.com", "no-reply@google.com") → SEMPRE delete
   * Notificações Google sobre "recuperação de conta", "termos de utilização", "política de inatividade" → delete
   * Google Play notifications → delete
   * Spam/conteúdo pessoal irrelevante → delete
   * Cold outreach comercial (Stance, Nespresso, SL Benfica, Kure App, WOME BOX, etc.) → delete
   * Autódromo promoções → delete
   * PUBLICIDADE / PROMOS / OFERTAS COMERCIAIS → delete (critério David 21/4/2026)
   * LISTAS DE DISTRIBUIÇÃO NÃO DIRIGIDAS A MIM → delete (critério David 21/4/2026)
   ⚠️ NUNCA "delete" para: Notion (usa keep), Adobe payment refused (usa keep), Anthropic/SaaS (usa invoice).

5. "keep" — Ambíguo, mantém em inbox para revisão manual.
   ESTE É O DEFAULT quando há dúvida. O David prefere ver um email a mais
   na inbox do que perder um email importante no arquivo.
   Exemplos:
   * Notion "login num novo dispositivo" → keep (pode ser real)
   * Segurança Social "tem novas mensagens" → keep (até David abrir portal)
   * Adobe "pagamento recusado" → keep (precisa confirmar se foi acção do David)
   * Email de pessoa real dirigido ao David (mesmo que pareça FYI) → keep
   * Qualquer ambiguidade → keep

REGRAS FINAIS:
- Default é keep. Em dúvida, keep.
- Em dúvida entre delete e keep → keep (mais seguro)
- Em dúvida entre archive e keep → keep (David ainda não viu o email)
- Em dúvida entre archive e invoice → invoice (mais seguro para histórico fiscal)
- Só decide archive quando é claramente newsletter, rede social, ou notificação automática sem acção

Output OBRIGATÓRIO: JSON válido, APENAS isto, SEM markdown nem preamble:
{"classification": "actionable"|"invoice"|"archive"|"delete"|"keep", "reason": "frase curta PT", "confidence": 0.0-1.0}"""


def _strip_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
    m_start = text.find("{")
    m_end = text.rfind("}")
    if m_start != -1 and m_end > m_start:
        return text[m_start : m_end + 1]
    return text


VALID_CLASSES = {"actionable", "invoice", "archive", "delete", "keep"}

# =========================================================================
# Enderecos do David (usados para "direct to me" check)
# =========================================================================
DAVID_ADDRESSES = {
    "david.sardinha@omnai.pt",
    "david.sardinha@previnsa.com",
    "david.sardinha@jmsoares.pt",
    "david.sardinha@sapo.pt",
    "davidsardinhalves@gmail.com",
    "hello@omnai.pt",
    "sopato.cascais@gmail.com",
    "opaidapetinga@gmail.com",
}

# =========================================================================
# v0.8.2: regras aprendidas (lidas de /secrets/learned_rules.json)
# =========================================================================
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
LEARNED_RULES_FILE = SECRETS_DIR / "learned_rules.json"

_LEARNED_SENDERS: set[str] = set()
_LEARNED_DOMAINS: set[str] = set()


def reload_learned_rules() -> dict[str, int]:
    """Recarrega learned_rules.json. Devolve contagem para diagnostico."""
    global _LEARNED_SENDERS, _LEARNED_DOMAINS
    senders: set[str] = set()
    domains: set[str] = set()
    if LEARNED_RULES_FILE.exists():
        try:
            data = json.loads(LEARNED_RULES_FILE.read_text(encoding="utf-8"))
            senders = set((data.get("learned_senders") or {}).keys())
            domains = set((data.get("learned_domains") or {}).keys())
        except Exception as exc:
            log.warning("classifier.learned_rules_parse_failed", err=str(exc))
    _LEARNED_SENDERS = {s.lower() for s in senders}
    _LEARNED_DOMAINS = {d.lower() for d in domains}
    log.info(
        "classifier.learned_rules_loaded",
        senders=len(_LEARNED_SENDERS),
        domains=len(_LEARNED_DOMAINS),
    )
    return {"senders": len(_LEARNED_SENDERS), "domains": len(_LEARNED_DOMAINS)}


# Load inicial ao importar
reload_learned_rules()


def _matches_learned(from_addr: str) -> bool:
    """v0.8.2: True se o sender ou dominio bate em regra aprendida."""
    if not (_LEARNED_SENDERS or _LEARNED_DOMAINS):
        return False
    fr_l = (from_addr or "").lower()
    if not fr_l:
        return False
    addr = extract_email(from_addr).lower()
    if addr and addr in _LEARNED_SENDERS:
        return True
    if "@" in addr:
        domain = addr.split("@", 1)[1]
        if domain in _LEARNED_DOMAINS:
            return True
    return False


# =========================================================================
# Heuristicas pre-LLM (criterios do David, 21 Abr 2026, v0.8.2 28 Abr 2026)
# =========================================================================

# v0.8.0: Padroes de bulk/automation senders.
_BULK_SENDER_PATTERNS = [
    r"\bno[-_.]?reply\b",
    r"\bdo[-_.]?not[-_.]?reply\b",
    r"\bnoreply[-_.]",
    r"@no[-_.]?reply\.",
    r"\bnotifications?@",
    r"\bnotify[-_.]?",
    r"@notify\.",
    r"@notifications\.",
    r"\balerts?@",
    r"\bjob[-_.]?alerts?\b",
    r"\bjobalerts?[-_.]?noreply\b",
    r"\bnews[-_.]?letter[-_.]?",
    r"newsletters?[-_.]?noreply",
    r"\bcustomer[-_.]?(?:support|service|care)@",
    r"\bsupport[-_.]?(?:noreply|automated)?@",
    r"\bbilling[-_.]?notice@",
    r"\bfailed[-_.]?payments?@",
    r"\binvoice\+",
    r"\binvoicing@",
    r"\btransaction@notice\.",
    r"\bmailer[-_.]?daemon\b",
    r"\bgoogleplay[-_.]?noreply@",
    r"@accounts\.google\.com\b",
    r"@em-s\.dropbox\.com\b",
    r"@notify\.notion\.so\b",
    r"@updates\.notion\.so\b",
    r"@mail\.notion\.so\b",
    r"@notice\.aliexpress",
    r"@deals\.aliexpress",
    r"@email\.microsoft\.com\b",
    r"@email\.apple\.com\b",
    r"@members\.netflix\.com\b",
    r"@info\.hostinger\.com\b",
    r"@info\.surfshark\.com\b",
    r"@mail\.surfshark\.com\b",
    r"@account\.canva\.com\b",
    r"@mail\.bancobpi\.pt\b",
    r"@info\.facebookmail",
    r"@.*\.facebookmail\.com\b",
    r"@.*\.linkedin\.com\b",
    r"@.*\.linkedinmail\.com\b",
    r"@reply\.email\.microsoft",
    r"@mail\.quicken\.com\b",
    r"@info\.bidbybid\.pt\b",
    r"@.*\.booking\.com\b",
    r"@vwgroup\.io\b",
    r"@eu\.zoom\.us\b",
    r"@em\.expo\.dev\b",
    r"@invoicing\.resend",
    r"^team@",
    r"^info@",
    r"^hello@",
    r"^hi@",
    r"^contact@",
    r"^marketing@",
    r"^updates@",
]

_INVOICE_KEYWORDS = [
    "invoice", "fatura", "factura", "receipt", "recibo", "statement",
    "billing", "subscription", "renovacao", "renovação", "renewal",
    "payment received", "pagamento", "order confirmation",
    "confirmacao de encomenda", "confirmação de encomenda",
    "amount due", "valor a pagar", "transaction confirmation",
    "purchase confirmation", "your receipt", "your invoice", "the bill",
]

_PROMO_SUBJECT_PATTERNS = [
    r"\b\d{1,3}\s*%\s*(off|desconto)",
    r"\bblack\s+friday\b",
    r"\bcyber\s+monday\b",
    r"\b(ultima|ultimo)\s+(dia|dias|hora|horas|chance)\b",
    r"\b(promoc[aã]o|promo|sale|saldos?|oferta)\b",
    r"\bgratis\b|\bgratuito\b|\bfree\s+(trial|ship)",
    r"\b(cupom|cupao|voucher|coupon)\b",
    r"\b(desconto|oferta)\s+(exclusiv|especial)",
    r"\bnew\s+arrivals\b",
    r"[\U0001f525\U0001f389\U0001f381\U0001f6cd]",
]

_PROMO_SENDER_PATTERNS = [
    r"@(?:newsletter|mail|email|comms?|marketing|promo|promos|ofertas|sales?|deals|store)\.",
    r"(?:marketing|promo|promos|ofertas|deals|sales|noreply[-_]?(?:marketing|promo))@",
    r"@(?:qualtrics-research|sendinblue|mailchimp|klaviyo|constantcontact|mailerlite)",
    r"@(?:mail\.impresa|email\.marshall|email\.apple|newsletter\.aosom|mail\.floa)",
    r"@(?:nplusbikes|qualtrics|shelly\.cloud)\.",
    r"@(?:agora-escolha\.com|info\.agora-escolha)",
    r"@(?:eu\.stance\.com|help\.stance)",
    r"@deals\.aliexpress",
    r"@.*\.theresanaiforthat\.com",
]

_NEWSLETTER_SENDER_PATTERNS = [
    r"(?:newsletter|news|weekly|daily|digest)[-_.]",
    r"@(?:substack|medium)\.",
    r"newsletters?-noreply@",
    r"@(?:kinsta|anthropic|openai|vercel|supabase|github|notion).*\bnewsletter",
    r"executiveducation@",
    r"@(?:em-s\.dropbox|account\.canva|members\.netflix|info\.surfshark|mail\.surfshark|mail\.quicken|info\.hostinger)",
    r"@.*\.notion\.so\b",
    r"@.*\.expo\.dev\b",
    r"@.*\.deepstash\.com\b",
    r"@.*\.bidbybid\.pt\b",
    r"@.*\.bancobpi\.pt\b",
]

_SOCIAL_DOMAIN_PATTERNS = [
    r"@(?:[a-z0-9\-]+\.)?linkedin\.com\b",
    r"@(?:[a-z0-9\-]+\.)?linkedinmail\.com\b",
    r"@(?:[a-z0-9\-]+\.)?facebookmail\.com\b",
    r"@(?:[a-z0-9\-]+\.)?facebook\.com\b",
    r"@(?:[a-z0-9\-]+\.)?fb\.com\b",
    r"@(?:[a-z0-9\-]+\.)?x\.com\b",
    r"@(?:[a-z0-9\-]+\.)?twitter\.com\b",
    r"@(?:[a-z0-9\-]+\.)?instagram\.com\b",
    r"@(?:[a-z0-9\-]+\.)?threads\.net\b",
    r"@(?:[a-z0-9\-]+\.)?youtube\.com\b",
    r"@(?:[a-z0-9\-]+\.)?tiktok\.com\b",
    r"@(?:[a-z0-9\-]+\.)?discord\.com\b",
    r"@(?:[a-z0-9\-]+\.)?discordmail\.com\b",
    r"@(?:[a-z0-9\-]+\.)?reddit\.com\b",
    r"@(?:[a-z0-9\-]+\.)?pinterest\.com\b",
    r"@(?:[a-z0-9\-]+\.)?mastodon\.social\b",
    r"@(?:[a-z0-9\-]+\.)?bsky\.app\b",
    r"@(?:[a-z0-9\-]+\.)?bluesky\.social\b",
]


def _header_value(raw_headers: dict | None, name: str) -> str:
    if not raw_headers:
        return ""
    for k, v in raw_headers.items():
        if k.lower() == name.lower():
            return str(v)
    return ""


def _is_bulk_sender(from_addr: str) -> bool:
    fr_l = (from_addr or "").lower()
    if not fr_l:
        return False
    for pat in _BULK_SENDER_PATTERNS:
        if re.search(pat, fr_l):
            return True
    return False


def _has_invoice_keyword(subject: str, from_addr: str, body: str = "") -> bool:
    subj_l = (subject or "").lower()
    fr_l = (from_addr or "").lower()
    body_head = (body or "").lower()[:1000]
    for kw in _INVOICE_KEYWORDS:
        if kw in subj_l or kw in fr_l or kw in body_head:
            return True
    return False


def _david_is_direct_recipient(
    headers: dict | None,
    to_addr: str,
    david_addresses: set[str],
    from_addr: str = "",
) -> bool:
    if from_addr and _is_bulk_sender(from_addr):
        return False
    to_raw = (
        _header_value(headers, "To")
        + " "
        + _header_value(headers, "Cc")
    ).lower()
    if not to_raw.strip():
        return False
    for addr in david_addresses:
        if addr.lower() in to_raw:
            return True
    return False


def _parse_meeting_date(subj_l: str) -> date | None:
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?", subj_l)
    if m:
        try:
            d, mo = int(m.group(1)), int(m.group(2))
            y_grp = m.group(3)
            y = int(y_grp) if y_grp else date.today().year
            if y < 100:
                y += 2000
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return date(y, mo, d)
        except Exception:
            return None
    months = {
        "jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5, "mai": 5,
        "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "out": 10,
        "nov": 11, "dec": 12, "dez": 12,
    }
    m = re.search(
        r"(\d{1,2})\.?\s+(?:de\s+)?(jan|feb|fev|mar|apr|abr|may|mai|jun|jul|aug|ago|sep|set|oct|out|nov|dec|dez)\w*\.?\s*(?:de\s+)?(\d{2,4})?",
        subj_l,
    )
    if m:
        try:
            d = int(m.group(1))
            mo = months.get(m.group(2), 0)
            y_grp = m.group(3)
            y = int(y_grp) if y_grp else date.today().year
            if y < 100:
                y += 2000
            if mo and 1 <= d <= 31:
                return date(y, mo, d)
        except Exception:
            return None
    return None


def heuristic_classify(
    from_addr: str,
    subject: str,
    body: str,
    headers: dict | None = None,
    to_addr: str = "",
    david_addresses: set[str] | None = None,
) -> dict[str, Any] | None:
    """v0.8.2: pipeline com regra `learned_from_trash` antes de bulk_default."""
    david_addresses = david_addresses or DAVID_ADDRESSES
    subj_l = (subject or "").lower()
    fr_l = (from_addr or "").lower()
    list_hdr = _header_value(headers, "List-Unsubscribe") or _header_value(headers, "List-ID")
    bulk_sender = _is_bulk_sender(from_addr)
    direct_to_david = _david_is_direct_recipient(
        headers, to_addr, david_addresses, from_addr
    )
    invoice_kw = _has_invoice_keyword(subject, from_addr, body)
    learned = _matches_learned(from_addr)

    # Regra -1: pre-check fatura.
    if invoice_kw and bulk_sender:
        return {
            "classification": "invoice",
            "reason": "heuristic.invoice_bulk_sender",
            "confidence": 0.85,
        }

    # Regra 0: David directo (genuino, sem bulk).
    if direct_to_david and not list_hdr:
        return {
            "classification": "keep",
            "reason": "heuristic.direct_to_david",
            "confidence": 0.95,
        }

    # Regra 1: promo subject (com guarda anti-fatura).
    for pat in _PROMO_SUBJECT_PATTERNS:
        if re.search(pat, subj_l):
            if invoice_kw:
                return {
                    "classification": "invoice",
                    "reason": "heuristic.promo_subject_invoice_guard",
                    "confidence": 0.8,
                }
            return {
                "classification": "delete",
                "reason": "heuristic.promo_subject",
                "confidence": 0.92,
            }

    # Regra 2: promo sender (com guarda anti-fatura).
    for pat in _PROMO_SENDER_PATTERNS:
        if re.search(pat, fr_l):
            if invoice_kw:
                return {
                    "classification": "invoice",
                    "reason": "heuristic.promo_sender_invoice_guard",
                    "confidence": 0.8,
                }
            return {
                "classification": "delete",
                "reason": "heuristic.promo_sender",
                "confidence": 0.88,
            }

    # Regra 3a: newsletter conhecido com List-ID.
    if list_hdr:
        for pat in _NEWSLETTER_SENDER_PATTERNS:
            if re.search(pat, fr_l):
                return {
                    "classification": "archive",
                    "reason": "heuristic.newsletter_known",
                    "confidence": 0.9,
                }

    # Regra 3b: redes sociais.
    for pat in _SOCIAL_DOMAIN_PATTERNS:
        if re.search(pat, fr_l):
            if direct_to_david:
                return {
                    "classification": "keep",
                    "reason": "heuristic.social_direct_inmail",
                    "confidence": 0.75,
                }
            return {
                "classification": "archive",
                "reason": "heuristic.social_notification",
                "confidence": 0.92,
            }

    # Regra 4: lista distribuicao nao dirigida a mim.
    if list_hdr and not direct_to_david:
        return {
            "classification": "delete",
            "reason": "heuristic.list_not_direct",
            "confidence": 0.85,
        }

    # Regra 4.5 (v0.8.2 NOVO): regras aprendidas com Lixo.
    # So actua se nao houver invoice keyword (faturas nunca apagadas).
    if learned and not invoice_kw:
        return {
            "classification": "delete",
            "reason": "heuristic.learned_from_trash",
            "confidence": 0.82,
        }

    # Regra 5: bulk sender default (v0.8.0).
    if bulk_sender:
        if invoice_kw:
            return {
                "classification": "invoice",
                "reason": "heuristic.bulk_with_invoice_kw",
                "confidence": 0.85,
            }
        return {
            "classification": "archive",
            "reason": "heuristic.bulk_sender_default",
            "confidence": 0.78,
        }

    # Regra 6: reuniao ja passada.
    if (
        "convite" in subj_l or "invite" in subj_l or "meeting" in subj_l
        or "reuniao" in subj_l or "reunião" in subj_l
    ):
        evt = _parse_meeting_date(subj_l)
        if evt and evt < date.today():
            return {
                "classification": "archive",
                "reason": f"heuristic.meeting_past:{evt.isoformat()}",
                "confidence": 0.85,
            }

    return None


async def classify(
    from_addr: str,
    subject: str,
    body: str,
    headers: dict | None = None,
    to_addr: str = "",
) -> dict[str, Any]:
    heur = heuristic_classify(from_addr, subject, body, headers, to_addr)
    if heur is not None:
        log.info(
            "classify.heuristic",
            cls=heur["classification"],
            reason=heur.get("reason"),
            from_=from_addr[:40],
        )
        return heur

    prompt = (
        f"From: {from_addr}\n"
        f"To: {to_addr}\n"
        f"Subject: {subject}\n\n"
        f"Body (truncado):\n{body[:2500]}\n\n"
        "Classifica seguindo as regras. O DEFAULT é 'keep'. Só decide 'archive' "
        "quando é CLARAMENTE newsletter, rede social ou notificacao automatica sem acção."
    )

    try:
        resp = await generate(system=SYSTEM_PROMPT, prompt=prompt, max_tokens=300)
    except Exception as exc:
        log.warning("classify.llm_failed", err=str(exc))
        return {"classification": "keep", "reason": "erro LLM", "confidence": 0.0}

    try:
        data = json.loads(_strip_json(resp))
        cls = data.get("classification", "keep")
        if cls not in VALID_CLASSES:
            cls = "keep"
        data["classification"] = cls
        return data
    except Exception as exc:
        log.warning("classify.parse_failed", err=str(exc), raw=resp[:200])
        return {
            "classification": "keep",
            "reason": f"parse fail: {resp[:80]}",
            "confidence": 0.0,
        }


def extract_email(from_addr: str) -> str:
    m = re.search(r"<([^>]+)>", from_addr)
    if m:
        return m.group(1).strip()
    m = re.search(r"[\w\.\-\+]+@[\w\.\-]+", from_addr)
    return m.group(0).strip() if m else from_addr.strip()
