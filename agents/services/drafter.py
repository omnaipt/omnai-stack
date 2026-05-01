"""Geracao de rascunhos de resposta via Claude.

Tom apropriado a cada caixa (Previnsa formal, OMNAI, Sopato, JMSoares, pessoal).
"""
from __future__ import annotations

from services.llm import generate


SYSTEM_PROMPT = """És o Carlos, Chief of Staff da OMNAI. Escreves rascunhos de resposta a emails em nome do David Sardinha. O David VAI rever e editar antes de enviar. Tu apenas preparas o terreno.

Regras de estilo:
- Português de Portugal, directo, profissional
- Sem travessões "—"; usa vírgulas, ponto e vírgula ou frase nova
- Sem clichés "não é sobre X, é sobre Y"
- Frases curtas com verbo
- Tom adequado: formal com clientes/fornecedores; mais directo com colegas próximos

Se faltar informação para responder completamente, marca os pontos a preencher com:
[EDITAR: descrição do que falta]

Estrutura típica:
- Cumprimento (Bom dia / Olá X / Caro Sr. X conforme contexto)
- Reconhecimento da mensagem
- Resposta ou próximo passo
- Despedida apropriada
- Assinatura: "David Sardinha" + (linha) + nome da empresa apropriada à caixa

NUNCA inventes factos, números, datas. Se não souberes, marca [EDITAR: ...].

Output: APENAS o texto do email. Sem subject line, sem preamble, sem comentários."""


_ACCOUNT_COMPANY = {
    "david.sardinha@previnsa.com": "Previnsa - Segurança e Vigilância, Lda",
    "david.sardinha@jmsoares.pt": "JMSoares",
    "david.sardinha@omnai.pt": "OMNAI",
    "hello@omnai.pt": "OMNAI",
    "davidsardinhalves@gmail.com": "OMNAI",
    "sopato.cascais@gmail.com": "Sopato Lda",
    "opaidapetinga@gmail.com": "Previnsa - Segurança e Vigilância, Lda",
    "david.sardinha@sapo.pt": "",  # pessoal, sem empresa
}


def company_for(account: str) -> str:
    return _ACCOUNT_COMPANY.get(account, "")


async def draft_response(
    account: str, from_addr: str, subject: str, body: str
) -> str:
    company = company_for(account)
    company_line = f"\n{company}" if company else ""

    prompt = (
        f"Caixa de recepção: {account}\n"
        f"Empresa associada: {company or '(pessoal)'}\n\n"
        f"Email recebido:\n"
        f"De: {from_addr}\n"
        f"Assunto: {subject}\n\n"
        f"Conteúdo:\n{body[:3500]}\n\n"
        f"Escreve o rascunho de resposta. Termina com a assinatura:\n"
        f"David Sardinha{company_line}"
    )
    return await generate(
        system=SYSTEM_PROMPT, prompt=prompt, max_tokens=1200
    )
