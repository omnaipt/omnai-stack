# -*- coding: utf-8 -*-
"""Extrai o texto de cada PDF do arquivo para um .txt, para servir de corpus."""
import os

from pypdf import PdfReader

RAIZ = os.getenv("FATURAS_DIR", "/faturas")
DESTINO = "/tmp/corpus"
os.makedirs(DESTINO, exist_ok=True)

n = 0
for pasta, _, ficheiros in os.walk(RAIZ):
    if os.path.basename(pasta).startswith("_"):
        continue
    for f in sorted(ficheiros):
        if not f.lower().endswith(".pdf"):
            continue
        caminho = os.path.join(pasta, f)
        try:
            r = PdfReader(caminho)
            t = "\n".join((p.extract_text() or "") for p in r.pages[:4])
        except Exception as exc:
            t = ""
        rel = os.path.relpath(caminho, RAIZ).replace(os.sep, "__")
        with open(os.path.join(DESTINO, rel + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(t)
        n += 1
print("%d textos escritos em %s" % (n, DESTINO))
