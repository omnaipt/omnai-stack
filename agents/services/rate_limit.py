"""Rate limiting por remetente para evitar que um unico contacto dispare
dezenas de drafts numa janela de scan.

Usa Redis (chave `omnai:email:rate:<date>:<from_domain>`) com TTL 48h.
Limite padrao: 3 drafts por dominio/dia. A decisao fica pre-registada para
ser visivel no briefing-carlos.
"""
from __future__ import annotations

import logging
from datetime import date
from email.utils import parseaddr

logger = logging.getLogger(__name__)


DEFAULT_LIMIT_PER_DOMAIN = 3
DEFAULT_TTL_SEC = 48 * 60 * 60  # 48 horas


def _extract_domain(from_header: str) -> str:
    _, addr = parseaddr(from_header or "")
    if "@" not in addr:
        return "unknown"
    return addr.split("@", 1)[1].lower().strip()


def should_draft(
    redis_client,
    from_header: str,
    *,
    limit: int = DEFAULT_LIMIT_PER_DOMAIN,
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> tuple[bool, int]:
    """Decide se vale a pena criar draft para este remetente.

    Devolve (allowed, current_count). Se allowed=False, current_count ja
    excede o limite e o caller deve registar como `skipped_rate_limit`.
    """
    domain = _extract_domain(from_header)
    key = f"omnai:email:rate:{date.today().isoformat()}:{domain}"

    try:
        current = redis_client.incr(key)
        if current == 1:
            redis_client.expire(key, ttl_sec)
        return (current <= limit, current)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("rate_limit check FAIL domain=%s err=%s", domain, exc)
        # Em caso de erro no Redis, nao bloqueamos.
        return (True, 0)
