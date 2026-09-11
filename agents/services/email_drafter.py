"""Geracao de draft de resposta a email ON-DEMAND (chamado pela UI Dashboard).

Poupa Claude: so corre quando o user clica 'Gerar resposta' no card do email.
Cacheia em email_inbox.draft_text para nao re-pagar.
"""
from __future__ import annotations

import logging

from services import email_inbox_db
from services.llm import generate

log = logging.getLogger(__name__)


SYSTEM_DRAFTER = (
    "Es o assistente de email do David Sardinha (CEO de OMNAI, Previnsa, JMSoares, Sopato). "
    "Escreves rascunhos de resposta em portugues europeu, tu directo, sem cliches, sem travessoes. "
    "Mantens o tom profissional mas humano. "
    "Se o email exige decisao do David que nao podes tomar, escreves [EDITAR: decidir X] no local certo. "
    "Maximo 150 palavras. Termina com 'Cumprimentos, David Sardinha' e a empresa correcta."
)


COMPANY_FROM_ACCOUNT = {
    "hello@omnai.pt": "OMNAI",
    "david.sardinha@omnai.pt": "OMNAI",
    "davidsardinhalves@gmail.com": "OMNAI",
    "sopato.cascais@gmail.com": "Sopato Lda",
    "opaidapetinga@gmail.com": "Previnsa, Lda",
    "david.sardinha@previnsa.com": "Previnsa, Lda",
    "david.sardinha@jmsoares.pt": "JMSoares, Lda",
    "david.sardinha@sapo.pt": "Pessoal",
}


async def generate_for(item_id: str) -> dict:
    """Gera draft para um email. Devolve {ok, draft_text|error}."""
    item = await email_inbox_db.get_by_id(item_id)
    if item is None:
        return {"ok": False, "error": "email nao encontrado"}

    if item.get("draft_text"):
        return {"ok": True, "draft_text": item["draft_text"], "cached": True}

    account = item.get("account", "")
    empresa = COMPANY_FROM_ACCOUNT.get(account, "OMNAI")
    subject = item.get("subject") or "(sem assunto)"
    from_addr = item.get("from_addr") or "(remetente desconhecido)"
    snippet = item.get("snippet") or ""

    prompt = (
        f"Email recebido:\n"
        f"De: {from_addr}\n"
        f"Assunto: {subject}\n"
        f"Conteudo (parcial): {snippet}\n\n"
        f"Escreve resposta em nome da {empresa}."
    )

    try:
        text = await generate(system=SYSTEM_DRAFTER, prompt=prompt, max_tokens=600)
    except Exception as exc:
        log.warning("draft FAIL id=%s err=%s", item_id, exc)
        return {"ok": False, "error": str(exc)[:200]}

    await email_inbox_db.save_draft(item_id, text)
    return {"ok": True, "draft_text": text, "cached": False}
