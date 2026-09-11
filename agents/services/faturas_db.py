"""Acesso ao indice de faturas (31-07-2026).

Estados:
  por_validar  chegou pelo arquivo automatico, ainda nao passou pelo olho do David
  validada     confirmada como despesa da empresa indicada, entra no pacote
  ignorada     nao e dele, e duplicado, ou nao e sequer uma fatura
  entregue     ja foi no pacote para a contabilidade

A validacao existe porque o arquivo automatico apanha coisas que nao sao dele:
apareceu uma fatura de 134.948,28 EUR que e da Alcainca. Sem este passo, um
numero desses entrava num fecho contabilistico.
"""
import os

import asyncpg

POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")
_pool: asyncpg.Pool | None = None

ESTADOS = ("por_validar", "validada", "ignorada", "entregue")
EMPRESAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not POSTGRES_DSN:
            raise RuntimeError("POSTGRES_DSN nao definido")
        _pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=3)
    return _pool


async def resumo() -> list[dict]:
    """Contagens e totais por mes e empresa. Duplicados nao contam para totais."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT to_char(mes,'YYYY-MM') AS mes, empresa,
                   count(*) FILTER (WHERE estado='por_validar') AS por_validar,
                   count(*) FILTER (WHERE estado='validada')    AS validadas,
                   count(*) FILTER (WHERE estado='entregue')    AS entregues,
                   count(*) FILTER (WHERE estado='ignorada')    AS ignoradas,
                   COALESCE(sum(valor) FILTER (
                       WHERE moeda='EUR' AND estado IN ('validada','entregue')), 0) AS eur,
                   COALESCE(sum(valor) FILTER (
                       WHERE moeda='USD' AND estado IN ('validada','entregue')), 0) AS usd
              FROM faturas
             WHERE mes IS NOT NULL
             GROUP BY 1,2
             ORDER BY 1 DESC, 2
            """
        )
        return [dict(r) for r in rows]


async def listar(mes: str | None = None, empresa: str | None = None,
                 estado: str | None = "por_validar", limit: int = 200) -> list[dict]:
    pool = await _get_pool()
    condicoes, args = [], []
    if mes:
        args.append(mes)
        condicoes.append(f"to_char(mes,'YYYY-MM') = ${len(args)}")
    if empresa:
        args.append(empresa)
        condicoes.append(f"empresa = ${len(args)}")
    if estado and estado != "todos":
        args.append(estado)
        condicoes.append(f"estado = ${len(args)}")
    args.append(limit)
    onde = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, empresa, to_char(mes,'YYYY-MM') AS mes, fornecedor,
                   data_fatura, valor, moeda, referencia, ficheiro, estado,
                   aviso, motivo, email_url, email_conta,
                   duplicado_de IS NOT NULL AS e_duplicado
              FROM faturas
              {onde}
             ORDER BY data_fatura DESC NULLS LAST, fornecedor
             LIMIT ${len(args)}
            """,
            *args,
        )
        return [dict(r) for r in rows]


async def accao(fatura_id: str, accao: str, empresa: str | None = None,
                motivo: str | None = None) -> bool:
    """validar, ignorar ou reatribuir a empresa de uma fatura."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if accao == "validar":
            sql = """UPDATE faturas
                        SET estado='validada', validada_em=now(), motivo=NULL,
                            empresa=COALESCE($2, empresa), actualizado_em=now()
                      WHERE id=$1::uuid AND estado <> 'entregue'"""
            r = await conn.execute(sql, fatura_id, empresa)
        elif accao == "ignorar":
            sql = """UPDATE faturas
                        SET estado='ignorada', motivo=COALESCE($2,'nao e minha'),
                            actualizado_em=now()
                      WHERE id=$1::uuid AND estado <> 'entregue'"""
            r = await conn.execute(sql, fatura_id, motivo)
        elif accao == "reabrir":
            sql = """UPDATE faturas
                        SET estado='por_validar', motivo=NULL, validada_em=NULL,
                            actualizado_em=now()
                      WHERE id=$1::uuid AND estado <> 'entregue'"""
            r = await conn.execute(sql, fatura_id)
        elif accao == "empresa":
            if empresa not in EMPRESAS:
                return False
            sql = """UPDATE faturas SET empresa=$2, actualizado_em=now()
                      WHERE id=$1::uuid AND estado <> 'entregue'"""
            r = await conn.execute(sql, fatura_id, empresa)
        else:
            return False
        return r.endswith(" 1")


async def para_pacote(empresa: str, mes: str) -> list[dict]:
    """Faturas validadas de uma empresa num mes, prontas a entregar."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, fornecedor, data_fatura, valor, moeda, referencia, ficheiro
              FROM faturas
             WHERE empresa = $1 AND to_char(mes,'YYYY-MM') = $2
               AND estado = 'validada'
             ORDER BY data_fatura, fornecedor
            """,
            empresa, mes,
        )
        return [dict(r) for r in rows]


async def marcar_entregues(ids: list[str]) -> int:
    if not ids:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            """UPDATE faturas SET estado='entregue', entregue_em=now(),
                      actualizado_em=now()
                WHERE id = ANY($1::uuid[]) AND estado='validada'""",
            ids,
        )
        try:
            return int(r.rsplit(" ", 1)[-1])
        except Exception:
            return 0
