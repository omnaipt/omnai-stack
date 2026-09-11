# -*- coding: utf-8 -*-
"""Sobe para o Drive tudo o que esta no arquivo local e nao esta la.

    docker exec -w /app omnai_agents python3 backfill_drive.py            # simula
    docker exec -w /app omnai_agents python3 backfill_drive.py --aplicar

As pastas que comecam por _ ficam de fora: os avisos de corte, os orcamentos
e os extractos nao vao para o arquivo da contabilidade. As entradas tambem
nao vao, por agora, porque a estrutura do Drive e de despesas; quando houver
decisao sobre elas, muda-se aqui.
"""
import os
import sys

sys.path.insert(0, "/app")

from services import drive  # noqa: E402

RAIZ = os.getenv("FATURAS_DIR", "/faturas")
APLICAR = "--aplicar" in sys.argv


def main():
    ligacao = drive.test_connection()
    if not ligacao.get("ok"):
        print("Drive inacessivel: %s" % ligacao.get("error"))
        return 1
    print("conta Drive: %s" % ligacao.get("email"))

    tarefas = []
    for pasta, _, ficheiros in os.walk(RAIZ):
        rel_pasta = os.path.relpath(pasta, RAIZ)
        if rel_pasta == ".":
            continue
        partes = rel_pasta.split(os.sep)
        if any(p.startswith("_") for p in partes):
            continue
        if len(partes) != 2:
            continue
        empresa, trimestre = partes
        for f in sorted(ficheiros):
            if f.lower().endswith(".pdf"):
                tarefas.append((empresa, trimestre, f, os.path.join(pasta, f)))

    print("%d ficheiros no arquivo local" % len(tarefas))
    if not APLICAR:
        for e, q, f, _ in tarefas[:8]:
            print("   %s/%s/%s" % (e, q, f))
        if len(tarefas) > 8:
            print("   ... mais %d" % (len(tarefas) - 8))
        print("\n(simulacao: nada foi enviado)")
        return 0

    novos, existiam, falhas = 0, 0, []
    for empresa, trimestre, nome, caminho in tarefas:
        try:
            with open(caminho, "rb") as fh:
                dados = fh.read()
            antes = drive.upload_bytes(dados, nome, empresa, trimestre)
            # o upload_bytes devolve o existente quando ja la esta
            if antes.get("webViewLink") and "id" in antes:
                # nao ha forma limpa de distinguir, conta-se pelo log
                novos += 1
        except Exception as exc:
            falhas.append((os.path.join(empresa, trimestre, nome),
                           "%s: %s" % (type(exc).__name__, str(exc)[:120])))
    print("\nprocessados : %d" % novos)
    if falhas:
        print("falhas      : %d" % len(falhas))
        for c, e in falhas[:15]:
            print("   %s\n      %s" % (c, e))
    return 0


sys.exit(main())
