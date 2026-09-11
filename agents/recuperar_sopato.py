# -*- coding: utf-8 -*-
"""Recupera as facturas da Sopato de Janeiro a Abril pelo caminho normal.

Nao ha aqui atalho nenhum: chama a mesma funcao que o worker chama quando
um email chega. Se isto funcionar, o pipeline funciona.
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from services import gmail  # noqa: E402
from workers.email_scan import _process_invoice_gmail  # noqa: E402

CONTA = "sopato.cascais@gmail.com"
QUERY = "from:aguasdecascais.pt after:2025/12/20 before:2026/04/15 has:attachment"
APLICAR = "--aplicar" in sys.argv


async def main():
    svc = gmail._service(CONTA)
    r = svc.users().messages().list(userId="me", q=QUERY, maxResults=40).execute()
    ids = [m["id"] for m in r.get("messages", [])]
    print("%d mensagens" % len(ids))

    for mid in ids:
        s = gmail.summarize_message(CONTA, mid)
        assunto = (s.get("subject") or "")[:52]
        if not APLICAR:
            print("  [simulacao] %s" % assunto)
            continue
        ok, motivo, saved = await _process_invoice_gmail(CONTA, s)
        destino = saved.get("path") if saved else "-"
        drive = "sim" if (saved or {}).get("drive_url") else "nao"
        print("  %-52s ok=%s motivo=%s" % (assunto, ok, motivo))
        print("      arquivo: %s" % destino)
        print("      subiu para o Drive: %s" % drive)

    if APLICAR:
        from services.faturas_index import indexar
        print("\nindice:", await indexar())


asyncio.run(main())
