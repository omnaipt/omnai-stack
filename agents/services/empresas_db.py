"""Dados de identificação das empresas (31-07-2026).

Modelo campo-a-campo em vez de colunas fixas: as quatro empresas estão em
estados muito diferentes (a Previnsa nem NIF português tem ainda) e a lista de
campos que interessa ter à mão vai mudar. Uma tabela de colunas fixas obrigaria
a uma migração por cada campo novo.
"""
import os

import asyncpg

POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")
_pool: asyncpg.Pool | None = None

EMPRESAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")

DDL = """
CREATE TABLE IF NOT EXISTS empresa_dados (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    empresa        text NOT NULL,
    campo          text NOT NULL,
    valor          text,
    ordem          int  NOT NULL DEFAULT 100,
    nota           text,
    actualizado_em timestamptz NOT NULL DEFAULT now(),
    UNIQUE (empresa, campo)
);
CREATE INDEX IF NOT EXISTS empresa_dados_idx ON empresa_dados (empresa, ordem);
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


async def listar() -> list[dict]:
    await garantir_tabela()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, empresa, campo, valor, ordem, nota
              FROM empresa_dados
             ORDER BY empresa, ordem, campo
            """
        )
        return [dict(r) for r in rows]


async def gravar(empresa: str, campo: str, valor: str | None,
                 ordem: int = 100, nota: str | None = None) -> bool:
    """Upsert de um campo. Valor vazio guarda NULL, para contar como lacuna."""
    await garantir_tabela()
    campo = (campo or "").strip()
    if not campo or empresa not in EMPRESAS:
        return False
    valor = (valor or "").strip() or None
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO empresa_dados (empresa, campo, valor, ordem, nota)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (empresa, campo) DO UPDATE SET
                valor = EXCLUDED.valor,
                nota = COALESCE(EXCLUDED.nota, empresa_dados.nota),
                actualizado_em = now()
            """,
            empresa, campo, valor, ordem, nota,
        )
        return True


async def apagar(dado_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute("DELETE FROM empresa_dados WHERE id=$1::uuid", dado_id)
        return r.rsplit(" ", 1)[-1] == "1"
