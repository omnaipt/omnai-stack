# -*- coding: utf-8 -*-
"""Uma compra, um cartao. Nao um cartao por email sobre a compra.

07-08-2026. A Dynadot mandou "Order Received (order 23204123)" e logo a
seguir "Order Finished (order 23204123)". Sao dois emails sobre a mesma
encomenda e apareceram dois cartoes iguais no Hoje.

A causa e a chave. Os cartoes de factura eram agrupados por assunto (ou pelo
id da mensagem), e o assunto muda a cada passo da encomenda. Qualquer
fornecedor que avise duas vezes gera dois cartoes.

A chave passa a ser, quando da, **o dominio do remetente mais a referencia
da encomenda**. Os dois emails da Dynadot passam a cair no mesmo cartao.

Conservador de proposito. So conta como referencia um numero que venha
**precedido de uma palavra que o identifica** (order, encomenda, factura,
recibo, pedido, referencia). Um numero solto no assunto nao serve: uma data,
um valor ou um numero de cliente colapsariam compras diferentes no mesmo
cartao, e ai o defeito seria perder trabalho em vez de duplicar cartoes.
Entre duplicar e esconder, duplicar e o erro menos caro.

Sem referencia reconhecida, mantem-se o comportamento antigo.
"""
from __future__ import annotations

import re

# Cada padrao exige a palavra que nomeia o numero. Ver docstring.
_PADROES = (
    r"\border\s*(?:number|no|n[.º°])?\s*[:#]?\s*([0-9]{4,})",
    r"\bencomenda\s*(?:n[.º°]?)?\s*[:#]?\s*([0-9]{4,})",
    r"\bpedido\s*(?:n[.º°]?)?\s*[:#]?\s*([0-9]{4,})",
    r"\b(?:invoice|fact?ura)\s*(?:n[.º°]?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{3,})",
    r"\brecibo\s*(?:n[.º°]?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{3,})",
    r"\b(?:ref|refer[eê]ncia)\s*(?:n[.º°]?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{5,})",
)

_RE = tuple(re.compile(p, re.IGNORECASE) for p in _PADROES)


def dominio_remetente(remetente: str) -> str:
    """O que sobra de "Dynadot Orders <orders@dynadot.com>" e dynadot.com."""
    m = re.search(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", remetente or "")
    return m.group(1).lower() if m else ""


def referencia(assunto: str) -> str:
    """A referencia da encomenda no assunto, ou vazio se nao houver certeza."""
    texto = assunto or ""
    for rx in _RE:
        m = rx.search(texto)
        if m:
            ref = (m.group(1) or "").strip(" .:#-/").upper()
            # Um "numero" com menos de 4 caracteres nao identifica nada.
            if len(ref) >= 4:
                return ref
    return ""


def chave_compra(assunto: str, remetente: str, fallback: str = "") -> str:
    """Parte de chave estavel para cartoes de factura.

    Devolve "dominio:referencia" quando reconhece a encomenda, e o fallback
    (assunto ou id da mensagem, como antes) quando nao reconhece.
    """
    ref = referencia(assunto)
    if not ref:
        return fallback or (assunto or "")
    dom = dominio_remetente(remetente) or "sem-dominio"
    return "%s:%s" % (dom, ref)
