# -*- coding: utf-8 -*-
"""Antes de mandar o David extrair um documento, ver se ha documento.

07-08-2026. Sobrava um falso positivo depois de arrumar as palavras-chave:
"David, voce adicionou um novo cartao para usar com o Google Pay". Nao vem
de nenhuma regra deterministica, vem do LLM, que leu um email cheio de
cartoes e pagamentos e concluiu factura.

Mexer no prompt ajuda mas nao prova nada: da vez seguinte pode decidir ao
contrario. Isto e uma guarda deterministica, e corre **so** no ponto onde o
estrago acontece: quando a extraccao falhou e se esta prestes a criar um
cartao P1 que diz "extrair manualmente o PDF e arquivar".

A pergunta que faz: **ha alguma prova de que existe um documento?**

  1. um anexo que parece documento;
  2. a palavra de factura no assunto, no remetente, ou no corpo com um
     montante (a mesma funcao que o classificador usa, de proposito: duas
     nocoes de "palavra de factura" acabavam por divergir);
  3. um link que nomeia um documento ou uma area de facturacao.

Sem nenhuma das tres, nao se cria cartao nenhum.

O que isto **nao** faz, de proposito: nao arquiva o email, nao o apaga, nao
muda a classificacao. O email fica na caixa de entrada exactamente onde
estava. Se eu estiver errado, o David ve o email; se eu estivesse a apagar,
o erro custava uma factura. A falha segura e deixar quieto.
"""
from __future__ import annotations

import re

# 19-08-2026: as imagens sairam. Um .png num email e quase sempre o
# logotipo do remetente, e isso dava prova de documento a qualquer
# newsletter. A factura fotografada existe, mas quase sempre traz palavra
# no assunto; e mesmo que nao traga, o email fica na caixa.
_EXT_DOC = (".pdf", ".xml", ".doc", ".docx", ".xls", ".xlsx", ".p7s", ".zip")

# 19-08-2026: o link tem de **nomear uma factura**. Antes bastava conter
# "document" ou "download", e "documentation" contem "document": uma
# newsletter da Apple Developer passava por isso. As fronteiras estao la
# para que voltar a acrescentar uma palavra generica nao reabra o buraco.
_RX_LINK_DOC = re.compile(
    r"https?://[^\s\"'<>]*(?<![a-z])"
    r"(?:invoices?|faturas?|facturas?|recibos?|receipts?|billing"
    r"|faturacao|facturacao|comprovativos?)"
    r"(?![a-z])",
    re.IGNORECASE,
)


def _tem_anexo_documento(anexos) -> bool:
    for a in anexos or []:
        if isinstance(a, dict):
            nome = str(a.get("filename") or a.get("name") or "")
        else:
            nome = str(a)
        if nome.lower().endswith(_EXT_DOC):
            return True
    return False


def ha_prova(subject: str, from_addr: str, body: str = "",
             anexos=None) -> tuple[bool, str]:
    """Devolve (ha_prova, porque). O porque vai para o log, para se medir."""
    if _tem_anexo_documento(anexos):
        return True, "anexo"

    from services.classifier import _has_invoice_keyword
    if _has_invoice_keyword(subject or "", from_addr or "", body or ""):
        return True, "palavra de factura"

    if _RX_LINK_DOC.search(body or ""):
        return True, "link para documento"

    return False, "sem prova de documento"
