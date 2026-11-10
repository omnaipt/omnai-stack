"""Regras de prioridade e envelhecimento do ecra Hoje (BKL-157, Fase 1).

Logica PURA e sem efeitos colaterais. Nao toca na DB. E partilhada por:
  * o dry-run (scripts/bkl157_dryrun.py),
  * os testes (tests/test_priority_rules.py),
  * e, na fase de implementacao, pelo passe de reclassificacao em producao.

Decisoes acordadas com o David (10-11-2026):
  1. Defeito deixa de ser P1. Emails e documentos contabilisticos entram a P2;
     sobem a P1 so por sinal explicito (prazo / remetente prioritario / <=5 dias
     do prazo contabilistico).
  2. Envelhecimento ancorado em criado_em (ou um first_seen congelado em
     metadata), NUNCA em actualizado_em: o re-upsert diario dos workers refresca
     actualizado_em e anularia a regra.
  3. Ciclos mensais recorrentes nao criam cartao novo por mes; consolidam num
     cartao vivo por (empresa, tipo) que lista os meses em atraso. Prioridade
     sobe com a idade do mes mais antigo. Nunca auto-fecho.
  4. Limite de 5 P0 em simultaneo e de DISPLAY: acima disso o briefing avisa, nao
     esconde (os itens continuam na DB).

Nada e apagado. "Arquivar" e uma mudanca de status reversivel (status=archived).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

# --- Constantes afinaveis (a spec do BKL-157) -------------------------------
AGING_STEP_DAYS = 21          # sem alteracao >= 21 dias -> desce um nivel
ARCHIVE_P3_DAYS = 30          # P3 sem alteracao >= 30 dias -> arquivado
P0_CAP = 5                    # maximo de P0 mostrados antes de avisar
CONTAB_DEADLINE_DAY = 9       # dia 9: pacote a contabilidade
CONTAB_P1_WINDOW_DAYS = 5     # <= 5 dias do prazo contabilistico -> P1

RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
INV = {v: k for k, v in RANK.items()}

# Familias de tipo
EMAIL_TIPOS = ("email_actionable",)
CONTAB_DOC_TIPOS = (
    "fatura_sem_documento", "fatura_destinatario", "recibo_falta",
    "fecho_sem_fatura", "resposta_contabilidade", "fecho_pacote",
)
# Documentos contabilisticos que NUNCA sao dispensados/arquivados
# automaticamente (regra do David, 10-11-2026): so descem de prioridade; saem
# quando o documento chega ou sao fechados a mao. fatura_destinatario incluido
# (confirmado 10-11: factura em nome/NIF errado, a reemitir antes do fecho).
NO_AUTO_DISMISS_TIPOS = (
    "fatura_sem_documento", "fatura_destinatario", "recibo_falta",
    "resposta_contabilidade", "fecho_sem_fatura", "fecho_pacote",
)
CICLO_TIPOS = ("fecho_contabilistico", "deadline_contabilidade")
# Tipos emitidos por workers desligados neste item (deal_stale -> worker
# pipeline-review-semanal desligado em BKL-157). Em dry-run saem da fila.
WORKER_DESLIGADO_TIPOS = (
    "deal_stale_silencioso", "deal_stale_atrasado", "deal_stale_sem_accao",
    "pipeline_review_semanal",
)

# Janela em que uma data extraida do email justifica P1 (David, 10-11-2026):
# so hoje ou futuro e dentro de N dias. Datas passadas descem a P3.
EMAIL_DEADLINE_WINDOW_DAYS = 7

# Clientes (remetentes prioritarios): dominios de clientes -> P1. Deve vir dos
# contactos dos CRMs (OMNAI + Previnsa); enquanto nao houver ligacao, lista
# provisoria confirmada pelo David. Comparacao por substring em metadata['from'].
PRIORITY_SENDERS: tuple[str, ...] = ("cm-mafra.pt",)

# Contabilista: emails a pedir documentos ou pagamentos equiparam-se a
# documentos contabilisticos protegidos (nunca dispensados; descem mas ficam).
ACCOUNTANT_SENDERS: tuple[str, ...] = ("eugest.pt",)

# Janela de contas a pagar: fatura/pedido de pagamento com data limite sobe a
# P1 a <= N dias do prazo (regra 2c do David).
PAYMENT_P1_WINDOW_DAYS = 5

# Prospeccao: remetente pede proposta/orcamento/reuniao/contacto comercial -> P1.
_PROSPECTING_RE = re.compile(
    r"(proposta|or[çc]amento|reuni[aã]o|marca[çc][aã]o|contacto\s+comercial|"
    r"pedido\s+de\s+contacto|or[çc]ar|cota[çc][aã]o|quote|proposal|meeting)", re.I)

# Pedido de pagamento / fatura (contas a pagar).
_PAYMENT_RE = re.compile(
    r"(pagamento|pagar|fatura|factura|iva\b|iuc\b|guia|aven[çc]a|"
    r"valores\s+em\s+aberto|por\s+pagar|d[ée]bito|invoice)", re.I)

# Marketing / promocoes: nunca justificam P1 (bloqueio por padrao, nao 1 a 1).
_MARKETING_FROM_RE = re.compile(
    r"(promotion@|promo@|newsletter|no-?reply|noreply|marketing@|news@|"
    r"mailer@|email\.|aliexpress|notacoes@)", re.I)
_MARKETING_SUBJ_RE = re.compile(
    r"(unsubscribe|desconto|subscription|renew(ed|al)?|renov(ar|a[çc][aã]o)|"
    r"promo[cç][aã]o|sale\b|off\b|cup[aã]o|voucher|descont)", re.I)

# Extraccao de datas: dd/mm[/aaaa] e "dd [de] <mes>[.] [de] aaaa" (mes por
# extenso ou abreviado, com ou sem "de"/ponto, como nos convites de calendario).
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_MES_NUM = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_DATE_PT_RE = re.compile(
    r"\b(\d{1,2})\s+(?:de\s+)?"
    r"(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*\.?"
    r"(?:\s+de)?\s*(\d{4})?", re.I)


# --- Helpers de leitura tolerante -------------------------------------------
def _meta(item: dict) -> dict:
    m = item.get("metadata")
    if isinstance(m, str):
        try:
            import json
            m = json.loads(m)
        except Exception:
            m = {}
    return m or {}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def anchor_dt(item: dict) -> datetime | None:
    """Relogio de envelhecimento: first_seen congelado se existir, senao
    criado_em. NUNCA actualizado_em (ver docstring do modulo)."""
    m = _meta(item)
    return _parse_dt(m.get("first_seen")) or _parse_dt(item.get("criado_em"))


def age_days(item: dict, today: datetime) -> int:
    a = anchor_dt(item)
    if a is None:
        return 0
    return (today - a).days


def origem(item: dict) -> str:
    t = item.get("tipo", "")
    if t in EMAIL_TIPOS or t.startswith("email"):
        return "email_protegido" if is_protegido(item) else "email"
    if t in CICLO_TIPOS:
        return "ciclo_mensal"
    if t in CONTAB_DOC_TIPOS:
        return "contabilistico_doc"
    if t in WORKER_DESLIGADO_TIPOS:
        return "pipeline_desligado"
    if t.startswith("concurso"):
        return "concursos"
    if "concorrencia" in t:
        return "concorrencia"
    return "outro"


# --- Deteccao de sinais de subida para P1 -----------------------------------
def _email_texto(item: dict) -> str:
    m = _meta(item)
    return " ".join(str(m.get(k, "")) for k in ("subject", "reason")) \
        + " " + str(item.get("titulo", ""))


def _email_marketing(item: dict) -> bool:
    frm = str(_meta(item).get("from", ""))
    subj = str(_meta(item).get("subject", "")) + " " + str(item.get("titulo", ""))
    return bool(_MARKETING_FROM_RE.search(frm) or _MARKETING_SUBJ_RE.search(subj))


def _email_datas(item: dict, today: datetime) -> list[date]:
    """Datas extraidas do subject/reason/titulo. Ano em falta assume o ano de
    hoje. Ignora valores invalidos."""
    texto = _email_texto(item)
    out: list[date] = []
    for d, mth, yr in _DATE_NUM_RE.findall(texto):
        try:
            ano = int(yr) if yr else today.year
            if ano < 100:
                ano += 2000
            out.append(date(ano, int(mth), int(d)))
        except Exception:
            pass
    for d, mesnome, yr in _DATE_PT_RE.findall(texto):
        try:
            ano = int(yr) if yr else today.year
            out.append(date(ano, _MES_NUM[mesnome.lower()[:3]], int(d)))
        except Exception:
            pass
    return out


def _email_remetente_prioritario(item: dict) -> bool:
    if not PRIORITY_SENDERS:
        return False
    frm = str(_meta(item).get("from", "")).lower()
    return any(s.lower() in frm for s in PRIORITY_SENDERS)


def _email_contabilista(item: dict) -> bool:
    frm = str(_meta(item).get("from", "")).lower()
    return any(s.lower() in frm for s in ACCOUNTANT_SENDERS)


def _email_prospeccao(item: dict) -> bool:
    return bool(_PROSPECTING_RE.search(_email_texto(item)))


def _email_pedido_pagamento(item: dict) -> bool:
    return bool(_PAYMENT_RE.search(_email_texto(item)))


def is_protegido(item: dict) -> bool:
    """Item que nunca e dispensado automaticamente: so desce de prioridade e
    sai quando resolvido a mao. Documentos contabilisticos, emails do
    contabilista, e pedidos de pagamento/fatura."""
    t = item.get("tipo", "")
    if t in NO_AUTO_DISMISS_TIPOS:
        return True
    if t in EMAIL_TIPOS or t.startswith("email"):
        return _email_contabilista(item) or _email_pedido_pagamento(item)
    return False


def _dias_para_prazo_contabilistico(today: datetime) -> int:
    """Dias ate ao proximo dia 9 (inclusive). 0 se hoje e dia 9."""
    d = today.date()
    if d.day <= CONTAB_DEADLINE_DAY:
        return CONTAB_DEADLINE_DAY - d.day
    # proximo mes
    ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return (date(ano, mes, CONTAB_DEADLINE_DAY) - d).days


# --- Decisao por item --------------------------------------------------------
@dataclass
class Decisao:
    item_id: str
    tipo: str
    origem: str
    empresa: str | None
    urgencia_antes: str
    urgencia_depois: str | None   # None => item sai da fila (consolidado/dismiss)
    accao: str                    # manter | reclassificar | envelhecer | arquivar | consolidar | dismiss
    motivo: str
    titulo: str = ""


def _descer(urg: str, niveis: int) -> str:
    return INV[min(3, RANK[urg] + max(0, niveis))]


def _base_reclassify(item: dict, today: datetime) -> tuple[str, str]:
    """Reclassifica a urgencia-base (defeitos novos) sem envelhecer ainda.
    Devolve (urgencia_base, motivo)."""
    t = item.get("tipo", "")
    urg = item.get("urgencia", "P2")

    if t in EMAIL_TIPOS or t.startswith("email"):
        if _email_marketing(item):
            return "P2", "email marketing/promocao -> P2 (bloqueado de P1)"
        datas = _email_datas(item, today)
        futuras = [dt for dt in datas if dt >= today.date()]

        # (2c + contabilista) contas a pagar / contabilista: PROTEGIDO.
        # Sobe a P1 a <=5 dias do prazo; senao fica P2 (mas nunca dispensado).
        if _email_contabilista(item) or _email_pedido_pagamento(item):
            jp = today.date() + timedelta(days=PAYMENT_P1_WINDOW_DAYS)
            if futuras and any(today.date() <= dt <= jp for dt in futuras):
                return "P1", "pagamento/contabilista a <=5 dias do prazo (protegido)"
            return "P2", "pagamento/contabilista protegido (sem prazo iminente) -> P2"

        # Data util: hoje..+7d -> P1 (prazo iminente ganha sempre). Data so
        # passada -> P3 (evento ja aconteceu; ganha a prospeccao/remetente).
        limite = today.date() + timedelta(days=EMAIL_DEADLINE_WINDOW_DAYS)
        if datas and any(today.date() <= dt <= limite for dt in datas):
            return "P1", "email com data dentro de 7 dias"
        if datas and not futuras:
            return "P3", "email com data ja passada -> P3 (envelhece)"

        # (2b) prospeccao comercial de remetente -> P1.
        if _email_prospeccao(item):
            return "P1", "prospeccao (proposta/orcamento/reuniao/contacto) -> P1"

        # (2a) cliente (dominio prioritario) -> P1.
        if _email_remetente_prioritario(item):
            return "P1", "email de cliente (remetente prioritario)"
        return "P2", "email sem sinal util -> P2"

    if t in CONTAB_DOC_TIPOS:
        dias = _dias_para_prazo_contabilistico(today)
        if dias <= CONTAB_P1_WINDOW_DAYS:
            return "P1", f"documento contabilistico a {dias} dias do prazo"
        return "P2", f"documento contabilistico a {dias} dias do prazo -> defeito P2"

    # concursos tem a sua propria escalacao por prazo (nao mexer aqui)
    return urg, "mantem urgencia de origem"


def decidir(item: dict, today: datetime, *, cumulative: bool = False) -> Decisao:
    """Decisao para um item que NAO e de ciclo mensal (esses vao por grupo)."""
    t = item.get("tipo", "")
    urg0 = item.get("urgencia", "P2")
    org = origem(item)
    base = Decisao(
        item_id=item.get("id", ""), tipo=t, origem=org,
        empresa=item.get("empresa"), urgencia_antes=urg0,
        urgencia_depois=urg0, accao="manter", motivo="",
        titulo=item.get("titulo", ""),
    )

    # Worker desligado: sai da fila (dismiss reversivel).
    if t in WORKER_DESLIGADO_TIPOS:
        base.urgencia_depois = None
        base.accao = "dismiss"
        base.motivo = "worker pipeline-review-semanal desligado (BKL-157)"
        return base

    # 1. Reclassificacao de defeito.
    urg, motivo = _base_reclassify(item, today)
    if urg != urg0:
        base.accao = "reclassificar"
    base.motivo = motivo

    # 2. Envelhecimento ancorado em criado_em/first_seen.
    a = age_days(item, today)
    if a >= AGING_STEP_DAYS:
        niveis = (a // AGING_STEP_DAYS) if cumulative else 1
        novo = _descer(urg, niveis)
        if novo != urg:
            urg = novo
            if base.accao == "manter":
                base.accao = "envelhecer"
            base.motivo += f"; envelhecido {niveis} nivel(is) por {a}d sem alteracao"

    # 3. Arquivo de P3 velho (reversivel). EXCEPTO documentos contabilisticos,
    # que nunca sao dispensados automaticamente: descem ate P3 e ficam la ate o
    # documento chegar ou fecho manual.
    if urg == "P3" and a >= ARCHIVE_P3_DAYS:
        if is_protegido(item):
            base.urgencia_depois = "P3"
            if base.accao == "manter":
                base.accao = "envelhecer"
            base.motivo += (f"; P3 ha {a}d mas item protegido NAO arquiva "
                            f"(fica ate documento/pagamento/fecho manual)")
            return base
        base.urgencia_depois = None
        base.accao = "arquivar"
        base.motivo += f"; P3 ha {a}d -> arquivado (reversivel)"
        return base

    base.urgencia_depois = urg
    return base


# --- Consolidacao dos ciclos mensais ----------------------------------------
def _ym(item: dict) -> str:
    return _meta(item).get("year_month") or (
        f"{_meta(item).get('ano')}-{str(_meta(item).get('mes')).zfill(2)}")


def _mes_em_atraso(item: dict) -> bool:
    """Um mes do ciclo esta 'em atraso'?

    - fecho_contabilistico: o cartao e de abertura do ciclo; conta sempre.
    - deadline_contabilidade: so conta se NAO estiver OK (metadata ok=False ou
      'OK' ausente do titulo). Os 'OK' nao escalam (aprovado pelo David).
    """
    if item.get("tipo") == "deadline_contabilidade":
        ok = _meta(item).get("ok")
        ok_bool = (ok is True) or (str(ok).strip().lower() == "true") \
            or ("ok" in str(item.get("titulo", "")).lower()
                and "atencao" not in str(item.get("titulo", "")).lower())
        return not ok_bool
    return True


def _meses_atraso(items: Iterable[dict]) -> int:
    """Quantos meses distintos (year_month) estao em atraso no grupo."""
    yms = {_ym(i) for i in items if _mes_em_atraso(i) and "None" not in _ym(i)}
    return len(yms)


def _urgencia_ciclo(meses: int) -> str:
    """Prioridade do cartao consolidado sobe com a idade do mes mais antigo.
    Sem meses em atraso -> P2 (informativo, nao escala)."""
    if meses >= 2:
        return "P0"
    if meses == 1:
        return "P1"
    return "P2"


def consolidar_ciclos(items: list[dict], today: datetime) -> list[Decisao]:
    """Colapsa os cartoes de ciclo mensal num cartao vivo por (empresa, tipo).

    Guarda o mais antigo EM ATRASO de cada grupo como o cartao sobrevivente (ou
    o mais antigo se nenhum estiver em atraso); os restantes sao 'consolidar'
    (status mudado, nunca apagado). A urgencia do sobrevivente reflecte quantos
    meses estao em atraso.
    """
    grupos: dict[tuple[str, str], list[dict]] = {}
    for i in items:
        if i.get("tipo") not in CICLO_TIPOS:
            continue
        chave = (i.get("empresa") or "", i.get("tipo") or "")
        grupos.setdefault(chave, []).append(i)

    out: list[Decisao] = []
    for (emp, tipo), grupo in grupos.items():
        atrasados = sorted((i for i in grupo if _mes_em_atraso(i)),
                           key=lambda x: anchor_dt(x) or today)
        grupo_ord = atrasados or sorted(grupo, key=lambda x: anchor_dt(x) or today)
        sobrevivente = grupo_ord[0]
        meses = _meses_atraso(grupo)
        urg = _urgencia_ciclo(meses)
        for i in grupo:
            d = Decisao(
                item_id=i.get("id", ""), tipo=tipo, origem="ciclo_mensal",
                empresa=emp, urgencia_antes=i.get("urgencia", "P2"),
                urgencia_depois=None, accao="consolidar", motivo="",
                titulo=i.get("titulo", ""),
            )
            if i is sobrevivente:
                d.urgencia_depois = urg
                d.accao = "manter" if i.get("urgencia") == urg else "reclassificar"
                d.motivo = (f"cartao vivo unico {emp}/{tipo}: {meses} mes(es) em "
                            f"atraso -> {urg}")
            else:
                d.motivo = f"consolidado no cartao vivo {emp}/{tipo} (nao apagado)"
            out.append(d)
    return out


# --- Passe completo ----------------------------------------------------------
@dataclass
class Resultado:
    decisoes: list[Decisao] = field(default_factory=list)

    def contagem(self, quando: str) -> dict[str, int]:
        c = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        for d in self.decisoes:
            u = d.urgencia_antes if quando == "antes" else d.urgencia_depois
            if u in c:
                c[u] += 1
        c["total"] = sum(c.values())
        return c

    def sobrevivos_por_origem(self, urg: str) -> dict[str, int]:
        c: dict[str, int] = {}
        for d in self.decisoes:
            if d.urgencia_depois == urg:
                c[d.origem] = c.get(d.origem, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def aplicar(items: list[dict], today: datetime, *, cumulative: bool = False) -> Resultado:
    """Aplica todas as regras a lista de itens abertos. Nao muta os itens."""
    res = Resultado()
    # Ciclos mensais: por grupo.
    res.decisoes.extend(consolidar_ciclos(items, today))
    # Restantes: item a item.
    for i in items:
        if i.get("tipo") in CICLO_TIPOS:
            continue
        res.decisoes.append(decidir(i, today, cumulative=cumulative))
    return res


def p0_cap_aviso(contagem_depois: dict[str, int]) -> str | None:
    n = contagem_depois.get("P0", 0)
    if n > P0_CAP:
        return (f"{n} itens P0 (acima do limite de {P0_CAP}). O briefing avisa "
                f"em vez de listar todos.")
    return None
