"""Gestao de labels Gmail especificas do OMNAI.

Resolve o gap identificado no STATE de Carlos: apos arquivar uma fatura
a mensagem ficava sem marca e o proximo scan voltava a processa-la,
gerando duplicados `_1.pdf`, `_2.pdf` na pasta local.

A solucao: depois de `save_invoice_pdf` + upload Drive OK, aplicamos a label
`OMNAI-Invoice-Archived` na mensagem original. A classificacao no scan
seguinte ignora tudo o que ja tem esta label.

IMAP equivalente: marcar com flag `\\Seen` + `OMNAI-Invoice-Archived`
custom flag. Nem todos os servers IMAP suportam custom flags; quando
nao suportam caimos na flag `\\Seen` combinada com arquivo para pasta
"OMNAI Invoices".
"""
from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


INVOICE_LABEL_NAME = "OMNAI-Invoice-Archived"
PROCESSED_LABEL_NAME = "OMNAI-Processed"
FEEDBACK_LABEL_NAME = "OMNAI-Product-Feedback"


@lru_cache(maxsize=32)
def ensure_label(gmail_service, label_name: str) -> str:
    """Devolve o labelId, criando a label se nao existir.

    `gmail_service` e o resource da Gmail API ja autenticado para a conta.
    """
    existing = gmail_service.users().labels().list(userId="me").execute()
    for lb in existing.get("labels", []):
        if lb.get("name") == label_name:
            return lb["id"]

    created = (
        gmail_service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    logger.info("ensure_label created name=%s id=%s", label_name, created["id"])
    return created["id"]


def apply_label(gmail_service, message_id: str, label_name: str) -> bool:
    """Aplica label na mensagem. True se OK, False em caso de erro."""
    try:
        label_id = ensure_label(gmail_service, label_name)
        gmail_service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()
        return True
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("apply_label FAIL msg=%s label=%s err=%s", message_id, label_name, exc)
        return False


def already_processed(message_payload: dict, label_name: str = INVOICE_LABEL_NAME) -> bool:
    """True se a mensagem ja foi marcada com a label dada.

    `message_payload` deve ser o resultado de `users.messages.get(format=metadata)`
    que inclui labelIds na raiz.
    """
    label_names = set(message_payload.get("labelIds", []))
    if not label_names:
        return False

    # A Gmail API devolve labelIds, nao nomes. Para evitar lookup extra,
    # aceitamos tambem nomes directamente (caller pode passar por metodo alternativo).
    return label_name in label_names or any(name.endswith(label_name) for name in label_names)


def query_with_exclude_label(base_query: str, label_name: str = INVOICE_LABEL_NAME) -> str:
    """Acrescenta `-label:<name>` ao search query para excluir mensagens ja tratadas."""
    exclusion = f'-label:"{label_name}"'
    if not base_query:
        return exclusion
    return f"{base_query} {exclusion}"
