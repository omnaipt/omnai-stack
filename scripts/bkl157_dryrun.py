#!/usr/bin/env python3
"""Dry-run do BKL-157 Fase 1: aplica as regras de prioridade/envelhecimento
aos itens abertos de hoje e mostra antes/depois. NAO escreve nada.

Fonte dos dados (read-only, por ordem):
  1. --snapshot <ficheiro.json>  (formato do endpoint /api/inbox/items)
  2. --url https://agents.omnai.pt/api/inbox/items  (default)

Nunca muta a DB. "arquivar"/"consolidar"/"dismiss" sao mudancas de status
reversiveis que ESTE script apenas simula; nenhum item e apagado.

Uso:
  python scripts/bkl157_dryrun.py --snapshot inbox.json
  python scripts/bkl157_dryrun.py --today 2026-11-10 --cumulative
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
from services import priority_rules as pr  # noqa: E402


def carregar(args) -> list[dict]:
    if args.snapshot:
        d = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    else:
        import urllib.request
        with urllib.request.urlopen(args.url, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
    items = d.get("items", d if isinstance(d, list) else [])
    return [i for i in items if i.get("status", "open") == "open"]


def _tab(c: dict[str, int]) -> str:
    return (f"P0={c['P0']:>3}  P1={c['P1']:>3}  P2={c['P2']:>3}  "
            f"P3={c['P3']:>3}  total={c['total']:>3}")


def correr(items: list[dict], today: datetime, cumulative: bool) -> None:
    res = pr.aplicar(items, today, cumulative=cumulative)

    antes = res.contagem("antes")
    depois = res.contagem("depois")
    modo = "cumulativo" if cumulative else "um nivel (one-shot)"
    print(f"\n{'='*66}\nDRY-RUN BKL-157  |  hoje={today.date()}  |  envelhecimento={modo}")
    print(f"itens abertos: {len(items)}")
    print(f"{'='*66}")
    print(f"ANTES : {_tab(antes)}")
    print(f"DEPOIS: {_tab(depois)}")

    # Invariante: nada apagado. Toda a entrada tem exactamente uma decisao.
    ids_in = [i.get("id") for i in items]
    ids_dec = [d.item_id for d in res.decisoes]
    assert len(ids_dec) == len(ids_in), (
        f"FALHA INVARIANTE: {len(ids_in)} itens -> {len(ids_dec)} decisoes")
    assert set(ids_dec) == set(ids_in), "FALHA INVARIANTE: ids nao batem"
    por_accao = collections.Counter(d.accao for d in res.decisoes)
    print(f"\nacoes: {dict(por_accao)}")
    print("  (arquivar/consolidar/dismiss = mudanca de status reversivel, "
          "nenhum item apagado)")

    aviso = pr.p0_cap_aviso(depois)
    print(f"\nP0: {depois['P0']}  {'AVISO: '+aviso if aviso else '(dentro do limite)'}")

    print(f"\nP1 sobreviventes por origem ({depois['P1']} total):")
    for org, n in res.sobrevivos_por_origem("P1").items():
        print(f"   {n:>3}  {org}")

    print("\nLista dos P1 sobreviventes (titulo | origem | motivo):")
    p1 = [d for d in res.decisoes if d.urgencia_depois == "P1"]
    for d in sorted(p1, key=lambda x: x.origem):
        print(f"   [{d.origem}] {d.titulo[:70]}")
        print(f"        -> {d.motivo}")

    # P0 sobreviventes (para o David ver que ciclos ficam)
    print(f"\nP0 sobreviventes ({depois['P0']}):")
    for d in [x for x in res.decisoes if x.urgencia_depois == "P0"]:
        print(f"   [{d.origem}] {d.empresa} | {d.titulo[:60]} -> {d.motivo}")

    # Ajuda para a lista de remetentes prioritarios
    emails = [i for i in items if pr.origem(i) == "email"]
    froms = collections.Counter()
    for i in emails:
        froms[str(pr._meta(i).get("from", ""))[:50]] += 1
    print(f"\nRemetentes dos {len(emails)} emails abertos (para escolher "
          f"PRIORITY_SENDERS):")
    for f, n in froms.most_common(12):
        print(f"   {n:>3}  {f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot")
    ap.add_argument("--url", default="https://agents.omnai.pt/api/inbox/items")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: hoje UTC)")
    ap.add_argument("--cumulative", action="store_true")
    ap.add_argument("--both", action="store_true", help="corre one-shot e cumulativo")
    args = ap.parse_args()

    today = (datetime.fromisoformat(args.today).replace(tzinfo=timezone.utc)
             if args.today else datetime.now(timezone.utc))
    items = carregar(args)

    if args.both:
        correr(items, today, cumulative=False)
        correr(items, today, cumulative=True)
    else:
        correr(items, today, cumulative=args.cumulative)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
