# -*- coding: utf-8 -*-
"""Distingue um documento fiscal de uma carta de cobranca.

03-08-2026. Motivo: o arquivo da Sopato estava a guardar avisos de corte da
Aguas de Cascais como se fossem faturas. Os dois documentos vem do mesmo
fornecedor, no mesmo mes, com o mesmo numero de documento e o mesmo valor.
A diferenca nao esta nas palavras "corte" ou "divida": a propria fatura da
Aguas de Cascais traz, em rodape, "nao liquidar a fatura ate ao seu
vencimento podera originar corte de agua" e ate uma linha de "Encargo Aviso
Corte". Filtrar por essas palavras rejeitaria as faturas verdadeiras.

A diferenca real e outra, e e legal, nao textual: em Portugal um documento
fiscal e obrigado a identificar-se como tal. Traz ATCUD desde 2023, traz o
numero do programa certificado pela AT, traz decomposicao de IVA. Uma carta
de cobranca nao traz nada disso, porque nao e um documento fiscal: e uma
carta que *cita* documentos fiscais numa tabela.

Daqui sai a regra: marcas fiscais mandam. Se o documento se identifica como
documento fiscal, e fatura mesmo que fale de dividas e de cortes. Se nao se
identifica e usa linguagem de cobranca, e uma carta.

Faturas estrangeiras (Anthropic, Supabase, Resend) nao tem ATCUD. Ficam
cobertas porque a linguagem de cobranca aqui procurada e portuguesa e
especifica, logo nao dispara, e o titulo "Invoice" chega para as classificar.
"""
from __future__ import annotations

import re
import unicodedata

TIPO_FATURA = "fatura"
TIPO_NOTA_DEBITO = "nota_lancamento_debito"
TIPO_ENTRADA = "entrada_ou_recebimento"
TIPO_COBRANCA = "aviso_cobranca"
TIPO_ORCAMENTO = "orcamento_ou_proposta"
TIPO_EXTRATO = "extracto_bancario"
TIPO_COMUNICACAO = "comunicacao_contabilidade"
TIPO_INDETERMINADO = "indeterminado"

CUSTO = "custo"
ENTRADA = "entrada"
NAO_APLICAVEL = "nao_aplicavel"

# 03-08-2026, a pedido do David: uma nota de lancamento do banco tanto pode
# ser um custo (comissoes, imposto do selo, encargos) como uma entrada de
# dinheiro (um subsidio, uma transferencia recebida). O documento e o mesmo,
# muda o sentido do movimento, e so o vocabulario o denuncia depois de o PDF
# perder as colunas na extraccao de texto.
MARCAS_ENTRADA = [
    ("aviso_pagamento", r"aviso de pagamento", 6),
    ("confirming", r"\bconfirming\b|\bfactoring\b", 4),
    ("papel_de_fornecedor", r"nome do fornecedor|na qualidade de fornecedor", 4),
    ("ordenante", r"\bordenante\b|instrucoes recebidas", 4),
    ("dinheiro_a_entrar", r"\bcreditamos\b|transferencia recebida|"
                          r"\bsubsidio\b|\bsubsidios\b|\bajudas\b|"
                          r"montante creditado|valor a receber", 4),
]

MARCAS_CUSTO_BANCARIO = [
    ("comissoes", r"\bcomissao\b|\bcomissoes\b|encargos bancarios|"
                  r"despesas bancarias", 5),
    ("imposto_selo", r"imposto do selo", 4),
    ("manutencao", r"manutencao de conta|comissao de manutencao", 4),
    ("dinheiro_a_sair", r"\bdebitamos\b|valor debitado|a debitar", 4),
    ("juros_devedores", r"juros devedores", 3),
]


def _norm(texto: str) -> str:
    """Minusculas, sem acentos e com espacos colapsados.

    O texto sai de PDFs com quebras de linha em sitios arbitrarios, por isso
    tudo o que e espaco passa a um unico espaco antes de procurar padroes.
    """
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


# Marcas de que o documento SE IDENTIFICA como documento fiscal.
MARCAS_FISCAIS = [
    ("atcud", r"\batcud\b", 5),
    ("programa_certificado", r"processado por programa certificado", 5),
    ("titulo_fatura", r"\bfatura\s*n[.ºo°]|\bfactura\s*n[.ºo°]|"
                     r"\binvoice\s*(?:n[.ºo°]|no\b|number|#)", 4),
    # 03-08-2026: metade dos fornecedores nao escreve "Fatura nº X", escreve
    # "Numero da fatura: X". Sem isto ficavam todos por decidir.
    ("numero_da_fatura", r"n[uú]?mero da (?:fatura|factura)|"
                         r"n[uú]?mero do recibo|\brecibo\s*n[.ºo°]", 4),
    ("recibo_pagamento", r"received payment|payment receipt|"
                         r"data do pagamento|thanks for your business", 3),
    ("identificacao_fiscal", r"n[.º]{0,3}\s*de iva|vat\W{0,4}(?:gst)?\W{0,20}"
                             r"(?:identification\s*)?number|\bnipc\b|\bnif\b", 2),
    ("processado_computador", r"processado (?:por|em) computador", 2),
    ("data_da_fatura", r"data da (?:fatura|factura)|data de emiss", 1),
    ("fatura_recibo", r"\bfatura[ -]?recibo\b|\bfatura\s*/\s*recibo\b", 4),
    # Tem de ser o titulo. O aviso de pagamento do Millennium traz uma
    # legenda "NCA - Nota de Credito" e por causa dela passava por factura.
    ("nota_credito", r"nota de credito\s*n[.ºo°:]|\bcredit note\s*(?:n|#|:)", 4),
    ("valido_como_recibo", r"valido como recibo", 3),
    # A Via Verde emite um "extracto" que e, para todos os efeitos, a
    # factura das portagens. Di-lo por extenso, e a lei obriga-a a isso.
    ("valido_efeitos_fiscais", r"documento valido para efeitos fiscais", 5),
    ("iva_decomposto", r"iva a taxa de \d|base tributavel|total sem iva|"
                       r"\bvat\b.{0,20}\d", 3),
    ("periodo_faturacao", r"periodo de facturacao|periodo de faturacao|"
                          r"periodo faturado", 2),
    ("invoice_en", r"\binvoice\b", 2),
    ("vencimento", r"data de vencimento|due date", 1),
]

# Marcas de carta de cobranca. Uma carta CITA documentos, nao e um.
MARCAS_COBRANCA = [
    ("titulo_aviso_corte", r"aviso de corte|aviso previo de corte|"
                           r"aviso de suspensao", 6),
    ("valor_em_divida", r"valor total em divida|encontra\(?m?\)? ?-? ?se em divida|"
                        r"se encontram? em divida|divida vencida", 5),
    ("tabela_documentos_citados", r"documento\W{1,6}emissao\W{1,6}vencimento", 5),
    ("regularize", r"regularize o pagamento|regularizacao da divida|"
                   r"regularize a divida", 4),
    ("interrupcao", r"interrupcao do abastecimento|suspensao do fornecimento|"
                    r"interrupcao do fornecimento|proceder a interrupcao", 4),
    ("ref_correspondencia", r"referencia de correspondencia", 3),
    ("restabelecimento", r"restabelecimento implicara|religacao", 3),
    ("cobranca_judicial", r"cobranca judicial", 3),
    ("formato_carta", r"estimado\(a\) cliente|exmos? senhores", 1),
]

# Documentos que nao pertencem a contabilidade de todo. Ao contrario das
# cartas de cobranca, estes nem sequer representam uma despesa: um
# orcamento e uma intencao e um extracto e um espelho da conta.
MARCAS_NAO_CONTABILISTICAS = [
    # Titulo, e so vale se estiver no cabecalho: uma fatura verdadeira pode
    # dizer "conforme o nosso orcamento nº X" a meio e continua a ser fatura.
    ("titulo_orcamento", r"\bor[çc]amento\b|\bproposta\s*n[.ºo°]|"
                        r"\bcota[çc][ãa]o\s*n[.ºo°]|\bquotation\b|\bquote\s*n", 6,
     TIPO_ORCAMENTO, True),
    ("validade_proposta", r"validade da proposta|proposta v[aá]lida por", 5,
     TIPO_ORCAMENTO, False),
    ("titulo_extracto", r"extrato combinado|extracto combinado|"
                        r"extrato de conta|extracto de conta|"
                        r"resumo do extrato|resumo do extracto", 6,
     TIPO_EXTRATO, False),
    ("saldos", r"saldos credores|saldos devedores|dep[oó]sitos a ordem", 3,
     TIPO_EXTRATO, False),
    # A comunicacao trimestral da contabilidade lista faturas numa tabela,
    # ATCUD incluido. Sem esta regra, o ATCUD citado fazia-a passar por
    # factura, que e o mesmo erro do aviso de corte noutra roupagem.
    ("comunicacao_contabilista", r"assuntos pendentes|comunica[çc][ãa]o mensal|"
                                 r"detectamos no e-?fatura|detetamos no e-?fatura|"
                                 r"resolu[çc][ãa]o\s*/\s*resposta", 6,
     TIPO_COMUNICACAO, False),
    ("elaborado_verificado", r"elaborado por\s*:.{0,80}verificado por", 3,
     TIPO_COMUNICACAO, False),
]

CABECALHO = 400

# Especifico da Aguas de Cascais: os avisos saem com CTAVD no nome do
# ficheiro e as faturas nao. Sinal fraco de proposito, so desempata.
PADRAO_NOME_AVISO = re.compile(r"ctavd|aviso", re.I)


def _pontuar(texto_n: str, marcas) -> tuple[int, list[str]]:
    total = 0
    encontradas = []
    for nome, padrao, peso in marcas:
        if re.search(padrao, texto_n):
            total += peso
            encontradas.append(nome)
    return total, encontradas


def classificar(texto: str, nome_ficheiro: str = "") -> dict:
    """Devolve tipo, se e fatura, confianca e os sinais que decidiram.

    Os sinais vao no resultado de proposito: quando isto se enganar, tem de
    ser possivel ver com que fundamento se enganou, sem reproduzir o caso.
    """
    t = _norm(texto)
    if len(t) < 40:
        return {
            "tipo": TIPO_INDETERMINADO, "e_fatura": False, "confianca": 0.0,
            "natureza": NAO_APLICAVEL,
            "porque": "documento sem texto suficiente para decidir",
            "pontos_fiscais": 0, "pontos_cobranca": 0,
            "sinais_fiscais": [], "sinais_cobranca": [],
        }

    p_fiscal, s_fiscal = _pontuar(t, MARCAS_FISCAIS)
    p_cobranca, s_cobranca = _pontuar(t, MARCAS_COBRANCA)

    if PADRAO_NOME_AVISO.search(nome_ficheiro or ""):
        p_cobranca += 1
        s_cobranca.append("nome_do_ficheiro")

    p_entrada, s_entrada = _pontuar(t, MARCAS_ENTRADA)
    p_custo_banco, s_custo_banco = _pontuar(t, MARCAS_CUSTO_BANCARIO)
    e_nota_lancamento = bool(re.search(r"nota de lancamento", t))

    def _resposta(tipo, e_fatura, natureza, confianca, porque, sinais):
        return {
            "tipo": tipo, "e_fatura": e_fatura, "natureza": natureza,
            "confianca": round(confianca, 2), "porque": porque,
            "pontos_fiscais": p_fiscal, "pontos_cobranca": p_cobranca,
            "sinais_fiscais": s_fiscal, "sinais_cobranca": sinais,
        }

    # Nota de lancamento do banco: o mesmo impresso serve para os dois
    # sentidos, e a diferenca decide se e despesa ou receita.
    if e_nota_lancamento:
        if p_custo_banco > p_entrada:
            return _resposta(
                TIPO_NOTA_DEBITO, True, CUSTO, 0.85,
                "nota de lancamento a debito, e uma despesa bancaria: "
                + ", ".join(s_custo_banco), s_custo_banco)
        if p_entrada > p_custo_banco:
            return _resposta(
                TIPO_ENTRADA, False, ENTRADA, 0.85,
                "nota de lancamento a credito, e dinheiro a entrar: "
                + ", ".join(s_entrada), s_entrada)
        return _resposta(
            TIPO_INDETERMINADO, False, NAO_APLICAVEL, 0.3,
            "nota de lancamento sem indicacao clara do sentido do movimento. "
            "Vai para validacao manual.", s_entrada + s_custo_banco)

    # Documentos que anunciam dinheiro a entrar. Nao sao despesa, mas
    # tambem nao sao lixo: interessam do lado das receitas.
    if p_entrada >= 6 and p_entrada > p_fiscal:
        return _resposta(
            TIPO_ENTRADA, False, ENTRADA, min(0.95, 0.55 + 0.05 * p_entrada),
            "documento de recebimento, nao de despesa: " + ", ".join(s_entrada),
            s_entrada)

    # Documento que nem sequer e contabilistico. Avaliado primeiro porque o
    # titulo manda: um orcamento pode ter tudo o resto de uma fatura.
    p_nao, s_nao, sub = 0, [], None
    cabecalho = t[:CABECALHO]
    for nome, padrao, peso, subtipo, so_cabecalho in MARCAS_NAO_CONTABILISTICAS:
        alvo = cabecalho if so_cabecalho else t
        if re.search(padrao, alvo):
            p_nao += peso
            s_nao.append(nome)
            sub = sub or subtipo

    if p_nao >= 5 and p_nao > p_cobranca:
        return {
            "tipo": sub or TIPO_ORCAMENTO, "e_fatura": False,
            "natureza": NAO_APLICAVEL,
            "confianca": min(0.95, 0.6 + 0.05 * p_nao),
            "porque": "nao e documento contabilistico: " + ", ".join(s_nao),
            "pontos_fiscais": p_fiscal, "pontos_cobranca": p_cobranca,
            "sinais_fiscais": s_fiscal, "sinais_cobranca": s_nao,
        }

    tem_identidade_fiscal = bool({"atcud", "programa_certificado"} & set(s_fiscal))

    if tem_identidade_fiscal:
        tipo, e_fatura = TIPO_FATURA, True
        porque = ("o documento identifica-se como documento fiscal "
                  "(" + ", ".join(sorted({"atcud", "programa_certificado"} & set(s_fiscal))) + ")")
        confianca = 0.97
    elif p_cobranca >= 6 and p_cobranca > p_fiscal:
        tipo, e_fatura = TIPO_COBRANCA, False
        porque = "carta de cobranca: " + ", ".join(s_cobranca)
        confianca = min(0.95, 0.55 + 0.05 * (p_cobranca - p_fiscal))
    elif p_fiscal >= 4 and p_fiscal > p_cobranca:
        tipo, e_fatura = TIPO_FATURA, True
        porque = "marcas de documento fiscal: " + ", ".join(s_fiscal)
        confianca = min(0.9, 0.5 + 0.05 * (p_fiscal - p_cobranca))
    else:
        tipo, e_fatura = TIPO_INDETERMINADO, False
        porque = (f"sem decisao clara (fiscal={p_fiscal}, cobranca={p_cobranca}). "
                  "Vai para validacao manual.")
        confianca = 0.3

    return {
        "tipo": tipo,
        "e_fatura": e_fatura,
        "natureza": CUSTO if e_fatura else NAO_APLICAVEL,
        "confianca": round(confianca, 2),
        "porque": porque,
        "pontos_fiscais": p_fiscal,
        "pontos_cobranca": p_cobranca,
        "sinais_fiscais": s_fiscal,
        "sinais_cobranca": s_cobranca,
    }


# ---------------------------------------------------------------------------
# 11-09-2026: recibos e destinatario.
#
# Motivo: no fecho de Julho/Agosto a Eugest recebeu recibos de pagamento
# misturados com facturas (a Anthropic manda os dois PDFs no mesmo email e o
# arquivo guardava-os como "_1.pdf"), e tres facturas da Hostinger vinham em
# nome pessoal do David em vez da OMNAI. Nenhuma das duas coisas era visivel
# antes de a contabilista as apontar. Passam a ser detectadas no momento do
# arquivo.
# ---------------------------------------------------------------------------

NIF_EMPRESA = {
    "OMNAI": "519270592",
}
NOME_EMPRESA = {
    "OMNAI": r"omnai",
}
# NIFs e nomes que, numa factura de empresa, denunciam factura em nome pessoal.
NIF_PESSOAL = {"230791611": "David Sardinha"}
NOME_PESSOAL = r"david\s+(?:soares\s+)?sardinha(?:\s+alves)?"

MARCAS_RECIBO = [
    ("titulo_receipt", r"^\W*(?:receipt|recibo)\b", 5),
    ("receipt_number", r"receipt\s*(?:number|no\b|#)|recibo\s*n[.ºo°]", 4),
    ("payment_receipt", r"payment receipt|received payment|recibo de pagamento", 4),
    ("paid_on", r"\bpaid on\b|date paid|pago em|data do pagamento", 2),
    ("amount_paid", r"amount paid|valor pago|total pago", 2),
    ("thanks", r"thanks for your business|obrigado pelo seu pagamento", 1),
]
MARCAS_FATURA_FORTE = [
    ("invoice_number", r"invoice\s*(?:number|no\b|#)|\bfatura\s*n[.ºo°]|"
                       r"n[uú]?mero da (?:fatura|factura)", 4),
    ("due_date", r"due date|data de vencimento|amount due", 3),
    ("atcud", r"\batcud\b", 5),
]


def e_recibo(texto: str) -> dict:
    """Um recibo prova pagamento; a factura prova a despesa. So a segunda
    vai para a contabilidade. Um documento com marcas de recibo e sem marcas
    fortes de factura e recibo."""
    t = _norm(texto)
    p_rec, s_rec = _pontuar(t, MARCAS_RECIBO)
    p_fat, s_fat = _pontuar(t, MARCAS_FATURA_FORTE)
    # "Invoice number" impresso num recibo (Anthropic, Stripe) e referencia
    # a factura paga; nao chega para o promover a factura se o titulo diz
    # Receipt.
    titulo_recibo = "titulo_receipt" in s_rec or "receipt_number" in s_rec
    e_rec = p_rec >= 5 and (titulo_recibo or p_rec > p_fat)
    if "atcud" in s_fat:
        e_rec = False
    return {"e_recibo": e_rec, "pontos_recibo": p_rec, "pontos_fatura": p_fat,
            "sinais_recibo": s_rec, "sinais_fatura": s_fat}


def _nifs_no_texto(texto_n: str) -> set[str]:
    # NIF portugues: 9 digitos, opcionalmente com PT a frente e espacos.
    out = set()
    for m in re.finditer(r"(?<!\d)(?:pt\s*)?(\d{3})\s?(\d{3})\s?(\d{3})(?!\d)", texto_n):
        out.add("".join(m.groups()))
    return out


def verificar_destinatario(texto: str, empresa: str) -> str | None:
    """Devolve um aviso se a factura parece nao estar em nome da empresa.

    Regras, por ordem: NIF pessoal presente -> aviso; NIF da empresa presente
    em qualquer sitio (mesmo colado a outro numero, como na factura da Correia
    & Goncalves em que vem agarrado ao numero de conta) -> ok; nome pessoal
    presente e nome da empresa ausente -> aviso; NIF diferente do nosso logo
    a seguir ao nome da empresa (Resend facturou "Omnai ... PT519570592")
    -> aviso; fornecedor sem NIF nenhum e sem nome da empresa -> aviso fraco.
    O NIF do fornecedor nunca conta: so se olha para o que esta junto ao
    nome da empresa.
    """
    t = _norm(texto)
    nif_emp = NIF_EMPRESA.get(empresa)
    if not nif_emp:
        return None
    sem_espacos = re.sub(r"[\s.]", "", t)
    for nif, nome in NIF_PESSOAL.items():
        if nif in sem_espacos:
            return f"factura em nome pessoal ({nome}, NIF {nif}); pedir reemissao para {empresa} NIF {nif_emp}"
    if nif_emp in sem_espacos:
        return None
    padrao_emp = NOME_EMPRESA.get(empresa, empresa.lower())
    tem_nome_emp = bool(re.search(padrao_emp, t))
    tem_nome_pessoal = bool(re.search(NOME_PESSOAL, t))
    if tem_nome_pessoal and not tem_nome_emp:
        return f"factura em nome pessoal (David Sardinha), sem NIF da {empresa}; pedir reemissao"
    if tem_nome_emp:
        errados = set()
        for m in re.finditer(padrao_emp, t):
            janela = t[m.start(): m.start() + 160]
            for n in re.finditer(r"(?:\bnif\b|\bvat\b|tax id|contribuinte)\W{0,12}(?:pt\s*)?(\d{3}\s?\d{3}\s?\d{3})(?!\d)", janela):
                num = re.sub(r"\s", "", n.group(1))
                if num != nif_emp:
                    errados.add(num)
        if errados:
            return f"NIF errado na factura ({', '.join(sorted(errados))}); o da {empresa} e {nif_emp}"
        return None
    if not _nifs_no_texto(t):
        return f"factura sem NIF nem nome da {empresa}; confirmar que esta em nome da empresa"
    return None
