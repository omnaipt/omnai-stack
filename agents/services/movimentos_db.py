"""Movimentos bancarios (Revolut) e reconciliacao com o indice de faturas. 08-09-2026.

Tabela movimentos_bancarios: uma linha por perna (leg) de transaccao Revolut.
Categorias:
  despesa    saida com contraparte externa (cartao, transferencia); precisa de fatura
  receita    entrada
  interno    transferencia entre contas proprias (ex.: Main -> Poupanca); sem documento
  taxa       comissao Revolut
  juros      juros da poupanca / rewards
  cambio     troca de moeda entre contas proprias
  reembolso  card_refund
Estados de reconciliacao (match_estado):
  por_casar   ainda nao passou pelo matcher
  casado      fatura_id preenchido
  sem_fatura  despesa sem fatura no indice (e o que a contabilidade pergunta)
  nao_precisa interno, cambio, taxa, receita, reembolso
  ignorado    o David disse que nao precisa
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")
_pool: asyncpg.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS movimentos_bancarios (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fonte         text NOT NULL DEFAULT 'revolut',
    ext_id        text NOT NULL UNIQUE,
    tx_id         text NOT NULL,
    empresa       text NOT NULL DEFAULT 'OMNAI',
    conta_id      text,
    conta_nome    text,
    tipo          text,
    estado_tx     text,
    data          date NOT NULL,
    data_hora     timestamptz,
    valor         numeric(14,2) NOT NULL,
    moeda         text NOT NULL,
    valor_orig    numeric(14,2),
    moeda_orig    text,
    descricao     text,
    contraparte   text,
    referencia    text,
    categoria     text NOT NULL,
    match_estado  text NOT NULL DEFAULT 'por_casar',
    fatura_id     uuid REFERENCES faturas(id) ON DELETE SET NULL,
    nota          text,
    raw           jsonb,
    criado_em     timestamptz NOT NULL DEFAULT now(),
    actualizado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS movimentos_data_idx ON movimentos_bancarios (data);
CREATE INDEX IF NOT EXISTS movimentos_match_idx ON movimentos_bancarios (match_estado);
"""


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not POSTGRES_DSN:
            raise RuntimeError("POSTGRES_DSN nao definido")
        _pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=3)
    return _pool


async def garantir_tabela() -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DDL)


# ------------------------------------------------- normalizacao Revolut

def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _iso(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def categorizar(tx: dict, leg: dict, contas_proprias: set[str]) -> str:
    tipo = tx.get("type", "")
    amount = float(leg.get("amount") or 0)
    if tipo == "exchange":
        return "cambio"
    if tipo == "fee":
        return "taxa"
    if tipo in ("interest", "reward"):
        return "juros"
    if tipo == "card_refund":
        return "reembolso"
    if tipo == "transfer":
        legs = tx.get("legs") or []
        cp = (leg.get("counterparty") or {})
        if len(legs) >= 2 and all(l.get("account_id") in contas_proprias for l in legs):
            return "interno"
        if cp.get("account_id") in contas_proprias:
            return "interno"
        # heuristica: transferencia sem contraparte externa e descricao com nome de
        # conta propria (Main, Poupanca...) e movimento entre contas
        desc = (leg.get("description") or "").lower()
        if not cp and any(k in desc for k in ("poupan", "main", "savings", "pocket")):
            return "interno"
    if amount < 0:
        return "despesa"
    return "receita"


def legs_para_linhas(tx: dict, contas: dict[str, str]) -> list[dict]:
    """Uma linha por perna. contas: id -> nome (contas proprias)."""
    out = []
    created = _iso(tx.get("created_at"))
    completed = _iso(tx.get("completed_at")) or created
    for i, leg in enumerate(tx.get("legs") or []):
        cat = categorizar(tx, leg, set(contas))
        cp = leg.get("counterparty") or {}
        merchant = tx.get("merchant") or {}
        contraparte = merchant.get("name") or cp.get("name") or cp.get("account_id")
        out.append({
            "ext_id": leg.get("leg_id") or f"{tx['id']}:{i}",
            "tx_id": tx["id"],
            "conta_id": leg.get("account_id"),
            "conta_nome": contas.get(leg.get("account_id")),
            "tipo": tx.get("type"),
            "estado_tx": tx.get("state"),
            "data": (completed or datetime.utcnow()).date(),
            "data_hora": completed,
            "valor": _dec(leg.get("amount")) or Decimal("0"),
            "moeda": leg.get("currency") or "EUR",
            "valor_orig": _dec(leg.get("bill_amount")),
            "moeda_orig": leg.get("bill_currency"),
            "descricao": leg.get("description") or tx.get("reference"),
            "contraparte": contraparte,
            "referencia": tx.get("reference"),
            "categoria": cat,
            "match_estado": "por_casar" if cat == "despesa" else "nao_precisa",
            "nota": ("transferencia a socio/pessoa: precisa de documento (recibo, despesa, mutuo)"
                     if cat == "despesa" and tx.get("type") == "transfer"
                     and "sardinha" in (contraparte or "").lower() else None),
            "raw": json.dumps(tx, default=str),
        })
    return out


async def upsert_linhas(linhas: list[dict]) -> int:
    await garantir_tabela()
    pool = await _get_pool()
    n = 0
    async with pool.acquire() as conn:
        for l in linhas:
            r = await conn.execute(
                """
                INSERT INTO movimentos_bancarios
                  (ext_id, tx_id, conta_id, conta_nome, tipo, estado_tx, data, data_hora,
                   valor, moeda, valor_orig, moeda_orig, descricao, contraparte, referencia,
                   categoria, match_estado, raw, nota)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18::jsonb,$19)
                ON CONFLICT (ext_id) DO UPDATE SET
                  estado_tx = EXCLUDED.estado_tx, data = EXCLUDED.data,
                  data_hora = EXCLUDED.data_hora, descricao = EXCLUDED.descricao,
                  contraparte = COALESCE(EXCLUDED.contraparte, movimentos_bancarios.contraparte),
                  nota = COALESCE(movimentos_bancarios.nota, EXCLUDED.nota),
                  categoria = EXCLUDED.categoria,
                  match_estado = CASE
                      WHEN movimentos_bancarios.match_estado IN ('casado','ignorado','sem_fatura')
                       AND EXCLUDED.categoria = 'despesa'
                      THEN movimentos_bancarios.match_estado
                      ELSE EXCLUDED.match_estado END,
                  raw = EXCLUDED.raw, actualizado_em = now()
                """,
                l["ext_id"], l["tx_id"], l["conta_id"], l["conta_nome"], l["tipo"],
                l["estado_tx"], l["data"], l["data_hora"], l["valor"], l["moeda"],
                l["valor_orig"], l["moeda_orig"], l["descricao"], l["contraparte"],
                l["referencia"], l["categoria"], l["match_estado"], l["raw"], l.get("nota"),
            )
            n += 1
    return n


# ------------------------------------------------------- reconciliacao

_STOP = {"ltd", "lda", "inc", "llc", "sa", "s.a", "unipessoal", "the", "de", "da",
         "do", "com", "www", "pt", "eu", "us", "sub", "subscription"}


def _tokens(s: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2 and t not in _STOP}


def pontuar(mov: dict, fat: dict) -> float:
    """0 = nao casa. Regras:
    * valor e moeda batem (na moeda da conta ou na original bill_amount), ou
    * moedas diferentes sem bill_amount, mas fornecedor igual e racio EUR/USD
      plausivel (0.80 a 1.00): o Revolut nem sempre devolve bill_amount.
    * janela de datas: 12 dias para cartao, 45 para transferencia (as faturas
      pagas por transferencia pagam-se mais tarde)."""
    valor = abs(Decimal(str(mov["valor"])))
    cand = [(valor, mov["moeda"])]
    if mov.get("valor_orig") is not None:
        cand.append((abs(Decimal(str(mov["valor_orig"]))), mov.get("moeda_orig")))
    fv = Decimal(str(fat["valor"] or 0))
    if fv <= 0:
        return 0.0
    tf = _tokens(fat.get("fornecedor"))
    tm = _tokens(mov.get("contraparte")) | _tokens(mov.get("descricao"))
    mesmo_fornecedor = bool(tf and tm & tf)
    exacto = any(abs(v - fv) <= Decimal("0.02") and m == fat["moeda"] for v, m in cand)
    aproximado = False
    if not exacto:
        if not mesmo_fornecedor or fat["moeda"] == mov["moeda"]:
            return 0.0
        racio = float(valor / fv)
        if not (0.80 <= racio <= 1.00 or 1.00 <= racio <= 1.25):
            return 0.0
        aproximado = True
    janela = 45 if (mov.get("tipo") == "transfer") else 12
    score = 1.0
    if fat.get("data_fatura") and mov.get("data"):
        dias = abs((fat["data_fatura"] - mov["data"]).days)
        if dias > janela:
            return 0.0
        score += max(0.0, 1.0 - dias / janela)
    if mesmo_fornecedor:
        score += 2.0
    if aproximado:
        score -= 0.5
    return score


async def reconciliar(desde: date | None = None) -> dict:
    """Casa despesas por_casar/sem_fatura com faturas nao ignoradas e ainda livres."""
    await garantir_tabela()
    pool = await _get_pool()
    desde = desde or (date.today() - timedelta(days=120))
    async with pool.acquire() as conn:
        movs = await conn.fetch(
            """SELECT id, data, tipo, valor, moeda, valor_orig, moeda_orig, descricao, contraparte
                 FROM movimentos_bancarios
                WHERE categoria = 'despesa' AND match_estado IN ('por_casar','sem_fatura')
                  AND data >= $1""", desde)
        fats = await conn.fetch(
            """SELECT f.id, f.fornecedor, f.data_fatura, f.valor, f.moeda
                 FROM faturas f
                WHERE f.estado <> 'ignorada' AND f.valor IS NOT NULL
                  AND f.data_fatura >= $1
                  AND NOT EXISTS (SELECT 1 FROM movimentos_bancarios m
                                   WHERE m.fatura_id = f.id)""",
            desde - timedelta(days=15))
        fats = [dict(f) for f in fats]
        casados = sem = 0
        for m in movs:
            m = dict(m)
            melhor, melhor_s = None, 0.0
            for f in fats:
                s = pontuar(m, f)
                if s > melhor_s:
                    melhor, melhor_s = f, s
            if melhor and melhor_s >= 1.0:
                await conn.execute(
                    """UPDATE movimentos_bancarios SET match_estado='casado', fatura_id=$2,
                              actualizado_em=now() WHERE id=$1""", m["id"], melhor["id"])
                fats.remove(melhor)
                casados += 1
            else:
                await conn.execute(
                    """UPDATE movimentos_bancarios SET match_estado='sem_fatura',
                              actualizado_em=now() WHERE id=$1 AND match_estado <> 'sem_fatura'""",
                    m["id"])
                sem += 1
    return {"avaliados": len(movs), "casados": casados, "sem_fatura": sem}


async def listar(mes: str | None = None, desde: date | None = None, ate: date | None = None,
                 categoria: str | None = None, match_estado: str | None = None,
                 limit: int = 500) -> list[dict]:
    await garantir_tabela()
    pool = await _get_pool()
    cond, args = [], []
    if mes:
        args.append(mes)
        cond.append(f"to_char(m.data,'YYYY-MM') = ${len(args)}")
    if desde:
        args.append(desde)
        cond.append(f"m.data >= ${len(args)}")
    if ate:
        args.append(ate)
        cond.append(f"m.data <= ${len(args)}")
    if categoria:
        args.append(categoria)
        cond.append(f"m.categoria = ${len(args)}")
    if match_estado:
        args.append(match_estado)
        cond.append(f"m.match_estado = ${len(args)}")
    args.append(limit)
    onde = ("WHERE " + " AND ".join(cond)) if cond else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT m.id, m.data, m.conta_nome, m.tipo, m.estado_tx, m.valor, m.moeda,
                       m.valor_orig, m.moeda_orig, m.descricao, m.contraparte, m.referencia,
                       m.categoria, m.match_estado, m.nota, m.fatura_id,
                       f.fornecedor AS fatura_fornecedor, f.referencia AS fatura_ref,
                       f.ficheiro AS fatura_ficheiro
                  FROM movimentos_bancarios m
                  LEFT JOIN faturas f ON f.id = m.fatura_id
                  {onde}
                 ORDER BY m.data DESC, m.data_hora DESC
                 LIMIT ${len(args)}""", *args)
        return [dict(r) for r in rows]


async def marcar(mov_id: str, match_estado: str, fatura_id: str | None = None,
                 nota: str | None = None) -> bool:
    if match_estado not in ("casado", "sem_fatura", "nao_precisa", "ignorado", "por_casar"):
        return False
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            """UPDATE movimentos_bancarios
                  SET match_estado=$2, fatura_id=COALESCE($3::uuid, fatura_id),
                      nota=COALESCE($4, nota), actualizado_em=now()
                WHERE id=$1::uuid""", mov_id, match_estado, fatura_id, nota)
        return r.endswith(" 1")


async def resumo_mes(mes: str) -> dict:
    await garantir_tabela()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT categoria, match_estado, moeda, count(*) AS n, sum(valor) AS total
                 FROM movimentos_bancarios
                WHERE to_char(data,'YYYY-MM') = $1
                GROUP BY 1,2,3 ORDER BY 1,2,3""", mes)
        return {"mes": mes, "linhas": [dict(r) for r in rows]}
