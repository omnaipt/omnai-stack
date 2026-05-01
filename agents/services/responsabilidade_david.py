"""responsabilidade_david.py | Sprint 7.5

Define que combinacoes (tipo, empresa) merecem alertar o David no briefing.
Decisao dele: contabilidade so de OMNAI, Sopato e Pessoal. Previnsa
(via GSA italiano, country manager) e JMSoares (consultor externo) tem
contabilidade gerida por outras entidades.

Uso nos workers que emitem cards contabilisticos:

    from services.responsabilidade_david import is_alerta_relevante
    if not is_alerta_relevante(tipo="fecho_contabilistico", empresa="Previnsa"):
        return  # silently skip

Tipos de cards filtrados (apenas alertam para OMNAI, Sopato, Pessoal):
  - fecho_contabilistico
  - deadline_contabilidade
  - extracto_bancario_pend
  - fatura_arquivada
  - fatura_sem_documento
  - email_fatura_pendente
  - recibo_falta

Tipos NAO filtrados (continuam para todas as empresas):
  - email_actionable, concurso_novo, deal_stale_*, pipeline_review_semanal,
    deadline_legal, health_*
"""
from __future__ import annotations

EMPRESAS_CONTABILIDADE_DAVID: frozenset[str] = frozenset({"OMNAI", "Sopato", "Pessoal"})

TIPOS_CONTABILIDADE: frozenset[str] = frozenset({
    "fecho_contabilistico",
    "deadline_contabilidade",
    "extracto_bancario_pend",
    "fatura_arquivada",
    "fatura_sem_documento",
    "email_fatura_pendente",
    "recibo_falta",
})


def is_alerta_relevante(*, tipo: str, empresa: str) -> bool:
    """Devolve True se o tipo+empresa devem alertar o David.

    Para tipos contabilisticos, so alerta se empresa estiver em
    EMPRESAS_CONTABILIDADE_DAVID. Para outros tipos, alerta sempre.
    """
    if tipo in TIPOS_CONTABILIDADE and empresa not in EMPRESAS_CONTABILIDADE_DAVID:
        return False
    return True
