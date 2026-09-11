# -*- coding: utf-8 -*-
"""Para cada indeterminado: quanto texto saiu do PDF e o que la esta.

Distingue dois casos que se parecem no relatorio e pedem solucoes
opostas: PDF sem texto (precisa de OCR) e PDF com texto mas sem marcas
fiscais (precisa de melhores regras).
"""
import os
import sys

sys.path.insert(0, "/app")
from pypdf import PdfReader  # noqa: E402
from services.doc_fiscal import classificar  # noqa: E402

RAIZ = os.getenv("FATURAS_DIR", "/faturas")

for pasta, _, ficheiros in os.walk(RAIZ):
    if os.path.basename(pasta).startswith("_"):
        continue
    for f in sorted(ficheiros):
        if not f.lower().endswith(".pdf"):
            continue
        caminho = os.path.join(pasta, f)
        try:
            r = PdfReader(caminho)
            paginas = len(r.pages)
            t = "\n".join((p.extract_text() or "") for p in r.pages[:4])
        except Exception as exc:
            print("ERRO AO LER  %s  %s" % (f, exc))
            continue
        res = classificar(t, f)
        if res["tipo"] != "indeterminado":
            continue
        limpo = " ".join(t.split())
        print("%-58s paginas=%d  caracteres=%d" % (f[:58], paginas, len(limpo)))
        print("   inicio: %s" % (limpo[:180] if limpo else "(NENHUM TEXTO)"))
        print()
