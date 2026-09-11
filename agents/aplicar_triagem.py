# -*- coding: utf-8 -*-
"""Arruma o arquivo e poe a base de dados de acordo com o disco.

Move o que nao e fatura para uma pasta por motivo, e actualiza a linha
correspondente em faturas: estado ignorada, motivo visivel, e o caminho do
ficheiro corrigido para o botao "Ver PDF" continuar a funcionar.

    docker exec -w /app omnai_agents python3 aplicar_triagem.py            # simula
    docker exec -w /app omnai_agents python3 aplicar_triagem.py --aplicar  # actua

Nada e apagado. Um ficheiro mal classificado recupera-se movendo-o de volta.
"""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, "/app")

from pypdf import PdfReader  # noqa: E402

from services.briefing_db import _get_pool  # noqa: E402
from services.doc_fiscal import classificar  # noqa: E402

RAIZ = os.getenv("FATURAS_DIR", "/faturas")
APLICAR = "--aplicar" in sys.argv


def texto_pdf(caminho, paginas=4):
    try:
        r = PdfReader(caminho)
        return "\n".join((p.extract_text() or "") for p in r.pages[:paginas])
    except Exception:
        return ""


def pasta_para(r):
    if r.get("natureza") == "entrada":
        return "_entradas"
    return os.path.join("_nao_faturas", r["tipo"])


async def main():
    achados = []
    for pasta, _, ficheiros in os.walk(RAIZ):
        if os.path.basename(pasta).startswith("_"):
            continue
        for f in sorted(ficheiros):
            if not f.lower().endswith(".pdf"):
                continue
            caminho = os.path.join(pasta, f)
            r = classificar(texto_pdf(caminho), f)
            if r["tipo"] == "fatura":
                continue
            achados.append((os.path.relpath(caminho, RAIZ), r))

    if not achados:
        print("nada a arrumar")
        return

    for rel, r in achados:
        print("%-14s %s" % (r["tipo"], rel))
        print("   -> %s" % pasta_para(r))
        print("   %s" % r["porque"][:150])

    if not APLICAR:
        print("\n(simulacao: nada foi movido nem alterado)")
        return

    pool = await _get_pool()
    movidos, actualizados, sem_linha = 0, 0, []

    for rel, r in achados:
        novo_rel = os.path.join(pasta_para(r), rel)
        origem, destino = os.path.join(RAIZ, rel), os.path.join(RAIZ, novo_rel)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.move(origem, destino)
        movidos += 1

        motivo = "%s: %s" % (r["tipo"], r["porque"])
        async with pool.acquire() as conn:
            n = await conn.execute(
                """UPDATE faturas
                      SET estado = 'ignorada',
                          motivo = $2,
                          ficheiro = $3,
                          actualizado_em = now()
                    WHERE ficheiro = $1""",
                rel, motivo[:500], novo_rel)
        if n.rsplit(" ", 1)[-1] == "1":
            actualizados += 1
        else:
            sem_linha.append(rel)

    print("\nficheiros movidos      : %d" % movidos)
    print("linhas actualizadas    : %d" % actualizados)
    if sem_linha:
        print("sem linha na base (%d):" % len(sem_linha))
        for s in sem_linha:
            print("   %s" % s)


asyncio.run(main())
