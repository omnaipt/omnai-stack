# -*- coding: utf-8 -*-
"""Passa o arquivo todo pelo classificador. Por omissao nao muda nada.

Correr na VPS, dentro do container:
    docker exec -w /app omnai_agents python3 reclassificar_arquivo.py
    docker exec -w /app omnai_agents python3 reclassificar_arquivo.py --aplicar

Sem --aplicar limita-se a listar o que classificaria de outra forma. Com
--aplicar move os documentos que nao sao faturas para /faturas/_nao_faturas/
e marca a linha correspondente como ignorada, com o motivo. Nao apaga nada.
"""
import io
import os
import shutil
import sys

sys.path.insert(0, "/app")

from pypdf import PdfReader  # noqa: E402

from services.doc_fiscal import classificar  # noqa: E402

RAIZ = os.getenv("FATURAS_DIR", "/faturas")
QUARENTENA = os.path.join(RAIZ, "_nao_faturas")
APLICAR = "--aplicar" in sys.argv


def texto_pdf(caminho: str, paginas: int = 4) -> str:
    try:
        r = PdfReader(caminho)
        return "\n".join((p.extract_text() or "") for p in r.pages[:paginas])
    except Exception as exc:
        return ""


def main():
    suspeitos, faturas, sem_texto = [], 0, []
    for pasta, _, ficheiros in os.walk(RAIZ):
        if os.path.basename(pasta).startswith("_"):
            continue
        for f in sorted(ficheiros):
            if not f.lower().endswith(".pdf"):
                continue
            caminho = os.path.join(pasta, f)
            t = texto_pdf(caminho)
            r = classificar(t, f)
            rel = os.path.relpath(caminho, RAIZ)
            if r["tipo"] == "fatura":
                faturas += 1
            elif r["tipo"] == "indeterminado":
                sem_texto.append((rel, r))
            else:
                suspeitos.append((rel, r))

    print("faturas confirmadas : %d" % faturas)
    print("nao sao faturas     : %d" % len(suspeitos))
    print("por decidir          : %d" % len(sem_texto))
    print()
    for rel, r in suspeitos:
        print("NAO E FATURA  %s" % rel)
        print("   %s" % r["porque"])
    for rel, r in sem_texto:
        print("POR DECIDIR   %s" % rel)
        print("   %s" % r["porque"])

    if not APLICAR:
        print("\n(simulacao: nada foi movido. Correr com --aplicar para mover)")
        return

    for rel, r in suspeitos:
        origem = os.path.join(RAIZ, rel)
        destino = os.path.join(QUARENTENA, rel)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.move(origem, destino)
        print("movido: %s" % rel)
    print("\n%d documentos movidos para %s" % (len(suspeitos), QUARENTENA))
    print("As linhas na base de dados tem de ser marcadas a seguir, com")
    print("marcar_ignoradas.sql, para o ecra Faturas ficar coerente.")


main()
