"""Lista de tarefas do David (31-07-2026).

Escrever ou ditar uma linha e ela entra. O prazo e a empresa saem do proprio
texto quando la estao ("pagar seguro previnsa ate sexta"), porque a ditar
ninguem preenche formularios.
"""
import os
import re
import unicodedata
from datetime import date, datetime, timedelta

import asyncpg

POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL", "")
_pool: asyncpg.Pool | None = None

EMPRESAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")
_ALIAS_EMPRESA = {
    "omnai": "OMNAI", "previnsa": "Previnsa", "jmsoares": "JMSoares",
    "jm soares": "JMSoares", "jms": "JMSoares", "sopato": "Sopato",
    "pessoal": "Pessoal", "nostos": "OMNAI", "palaestra": "OMNAI",
}
_DIAS = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
         "sexta": 4, "sabado": 5, "domingo": 6}
_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
          "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
          "novembro": 11, "dezembro": 12}


def _sem_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower()


def interpretar(texto: str, hoje: date | None = None) -> dict:
    """Tira prazo e empresa do texto livre. O titulo fica intacto."""
    hoje = hoje or date.today()
    n = _sem_acentos(texto)
    prazo = None

    if re.search(r"\bhoje\b", n):
        prazo = hoje
    elif re.search(r"\bamanha\b", n):
        prazo = hoje + timedelta(days=1)
    elif re.search(r"\b(depois de amanha|dpa)\b", n):
        prazo = hoje + timedelta(days=2)
    elif re.search(r"\bproxima semana\b|\bpara a semana\b", n):
        prazo = hoje + timedelta(days=(7 - hoje.weekday()) or 7)
    elif re.search(r"\bfim do mes\b", n):
        seguinte = (hoje.replace(day=28) + timedelta(days=4)).replace(day=1)
        prazo = seguinte - timedelta(days=1)

    if prazo is None:
        m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", n)
        if m:
            dia, mes = int(m.group(1)), int(m.group(2))
            ano = int(m.group(3) or hoje.year)
            if ano < 100:
                ano += 2000
            try:
                prazo = date(ano, mes, dia)
            except ValueError:
                prazo = None

    if prazo is None:
        m = re.search(r"\bdia (\d{1,2})(?:\s+de\s+(\w+))?\b", n)
        if m:
            dia = int(m.group(1))
            mes = _MESES.get(m.group(2) or "", hoje.month)
            try:
                prazo = date(hoje.year, mes, dia)
                if prazo < hoje and not m.group(2):
                    prazo = (prazo.replace(day=1) + timedelta(days=32)).replace(day=dia)
            except ValueError:
                prazo = None

    if prazo is None:
        for nome, idx in _DIAS.items():
            if re.search(rf"\b{nome}(-feira)?\b", n):
                delta = (idx - hoje.weekday()) % 7 or 7
                prazo = hoje + timedelta(days=delta)
                break

    empresa = None
    for alias, valor in _ALIAS_EMPRESA.items():
        if re.search(rf"\b{alias}\b", n):
            empresa = valor
            break

    prioridade = "P1" if re.search(r"\burgente\b|\bp0\b", n) else "P2"
    return {"prazo": prazo, "empresa": empresa, "prioridade": prioridade}


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not POSTGRES_DSN:
            raise RuntimeError("POSTGRES_DSN nao definido")
        _pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=3)
    return _pool


async def listar(incluir_feitas: bool = False, limit: int = 300) -> list[dict]:
    pool = await _get_pool()
    where = "" if incluir_feitas else "WHERE status = 'open'"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, titulo, detalhe, prioridade, empresa, status, prazo,
                   criado_em, completado_em, metadata->>'notion_url' AS notion_url
              FROM user_todos
              {where}
             ORDER BY (prazo IS NULL), prazo, prioridade, criado_em
             LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def criar(texto: str, empresa: str | None = None,
                prazo: date | None = None) -> dict:
    """Cria a partir de uma linha. O que vier explicito ganha ao interpretado."""
    lido = interpretar(texto)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_todos (titulo, prioridade, empresa, status, prazo)
            VALUES ($1, $2, $3, 'open', $4)
            RETURNING id, titulo, prioridade, empresa, prazo, status, criado_em
            """,
            texto.strip()[:300], lido["prioridade"],
            empresa or lido["empresa"], prazo or lido["prazo"],
        )
        return dict(row)


async def accao(todo_id: str, accao: str, prazo: date | None = None,
                empresa: str | None = None) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if accao == "concluir":
            r = await conn.execute(
                """UPDATE user_todos SET status='done', completado_em=now()
                    WHERE id=$1::uuid AND status='open'""", todo_id)
        elif accao == "reabrir":
            r = await conn.execute(
                """UPDATE user_todos SET status='open', completado_em=NULL
                    WHERE id=$1::uuid""", todo_id)
        elif accao == "apagar":
            r = await conn.execute("DELETE FROM user_todos WHERE id=$1::uuid", todo_id)
        elif accao == "prazo":
            r = await conn.execute(
                "UPDATE user_todos SET prazo=$2 WHERE id=$1::uuid", todo_id, prazo)
        elif accao == "empresa":
            if empresa not in EMPRESAS:
                return False
            r = await conn.execute(
                "UPDATE user_todos SET empresa=$2 WHERE id=$1::uuid", todo_id, empresa)
        else:
            return False
        return r.rsplit(" ", 1)[-1] == "1"
