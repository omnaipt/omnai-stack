"""Testes das regras de prioridade/envelhecimento do BKL-157 (Fase 1).

Corre com pytest (CI) ou standalone:  python tests/test_priority_rules.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from services import priority_rules as pr  # noqa: E402

HOJE = datetime(2026, 11, 10, tzinfo=timezone.utc)


def _item(**kw) -> dict:
    base = {
        "id": kw.pop("id", "x"),
        "tipo": "email_actionable",
        "urgencia": "P1",
        "empresa": "OMNAI",
        "titulo": "t",
        "status": "open",
        "criado_em": HOJE.isoformat(),
        "actualizado_em": HOJE.isoformat(),
        "metadata": {},
    }
    base.update(kw)
    return base


# --- 1. Defeito passa a P2 --------------------------------------------------
def test_email_sem_sinal_cai_para_p2():
    it = _item(tipo="email_actionable", urgencia="P1",
               metadata={"from": "ruido@x.com", "subject": "ola"})
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois == "P2", d.motivo
    assert d.accao == "reclassificar"


def test_email_com_prazo_fica_p1():
    it = _item(metadata={"from": "x@x.com", "subject": "pagar ate dia 20"})
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois == "P1", d.motivo


def test_email_remetente_prioritario_fica_p1(monkeypatch=None):
    it = _item(metadata={"from": "Contabilidade <toc@x.pt>", "subject": "x"})
    pr.PRIORITY_SENDERS = ("toc@x.pt",)
    try:
        d = pr.decidir(it, HOJE)
        assert d.urgencia_depois == "P1", d.motivo
    finally:
        pr.PRIORITY_SENDERS = ()


def test_doc_contabilistico_fora_janela_cai_para_p2():
    # dia 10: proximo dia 9 esta a ~29 dias -> fora da janela de 5
    it = _item(tipo="fatura_sem_documento", urgencia="P1", metadata={})
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois == "P2", d.motivo


def test_doc_contabilistico_dentro_janela_fica_p1():
    # dia 6: proximo dia 9 esta a 3 dias -> dentro da janela
    dia6 = datetime(2026, 11, 6, tzinfo=timezone.utc)
    it = _item(tipo="recibo_falta", urgencia="P2", criado_em=dia6.isoformat(),
               metadata={})
    d = pr.decidir(it, dia6)
    assert d.urgencia_depois == "P1", d.motivo


# --- 2. Envelhecimento ancorado em criado_em, nunca actualizado_em ----------
def test_envelhece_por_criado_em():
    velho = (HOJE - timedelta(days=25)).isoformat()
    it = _item(tipo="outro_tipo", urgencia="P1", criado_em=velho,
               actualizado_em=velho)
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois == "P2"
    assert d.accao == "envelhecer"


def test_relogio_ignora_actualizado_em():
    """Item criado ha 40 dias mas re-tocado hoje: continua a envelhecer,
    porque o relogio e criado_em e nao actualizado_em."""
    criado = (HOJE - timedelta(days=40)).isoformat()
    it = _item(tipo="outro_tipo", urgencia="P1", criado_em=criado,
               actualizado_em=HOJE.isoformat())
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois == "P2", "re-upsert diario nao pode travar o envelhecimento"


def test_first_seen_congelado_tem_precedencia():
    criado = (HOJE - timedelta(days=40)).isoformat()
    first_seen = (HOJE - timedelta(days=5)).isoformat()  # congelado recente
    it = _item(tipo="outro_tipo", urgencia="P1", criado_em=criado,
               metadata={"first_seen": first_seen})
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois == "P1", "first_seen recente -> ainda nao envelhece"


def test_p3_velho_arquiva_reversivel():
    velho = (HOJE - timedelta(days=35)).isoformat()
    it = _item(tipo="outro_tipo", urgencia="P3", criado_em=velho,
               actualizado_em=velho)
    d = pr.decidir(it, HOJE)
    assert d.urgencia_depois is None
    assert d.accao == "arquivar"  # status muda, nao apaga


# --- 3. Deduplicacao de ciclos mensais --------------------------------------
def test_ciclos_consolidam_num_cartao_por_empresa_tipo():
    items = [
        _item(id="a", tipo="fecho_contabilistico", urgencia="P0", empresa="OMNAI",
              criado_em=datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat(),
              metadata={"year_month": "2026-08"}),
        _item(id="b", tipo="fecho_contabilistico", urgencia="P0", empresa="OMNAI",
              criado_em=datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
              metadata={"year_month": "2026-09"}),
        _item(id="c", tipo="fecho_contabilistico", urgencia="P0", empresa="Sopato",
              criado_em=datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
              metadata={"year_month": "2026-09"}),
    ]
    decs = pr.consolidar_ciclos(items, HOJE)
    assert len(decs) == 3  # nenhuma entrada perdida
    vivos = [d for d in decs if d.urgencia_depois is not None]
    # 1 cartao vivo para OMNAI, 1 para Sopato
    assert len(vivos) == 2
    omnai = next(d for d in vivos if d.empresa == "OMNAI")
    assert omnai.urgencia_depois == "P0"  # 2 meses em atraso
    assert "2 mes" in omnai.motivo
    consolidados = [d for d in decs if d.accao == "consolidar"]
    assert len(consolidados) == 1  # o segundo mes do OMNAI
    # nenhum apagado: toda a entrada tem decisao
    assert {d.item_id for d in decs} == {"a", "b", "c"}


def test_ciclo_um_mes_e_p1():
    items = [
        _item(id="a", tipo="deadline_contabilidade", urgencia="P0", empresa="OMNAI",
              metadata={"year_month": "2026-10"}),
    ]
    decs = pr.consolidar_ciclos(items, HOJE)
    assert len(decs) == 1
    assert decs[0].urgencia_depois == "P1"


# --- 4. Limite de 5 P0 ------------------------------------------------------
def test_p0_cap_avisa_acima_de_5():
    assert pr.p0_cap_aviso({"P0": 6}) is not None
    assert pr.p0_cap_aviso({"P0": 5}) is None
    assert pr.p0_cap_aviso({"P0": 0}) is None


# --- 5. Invariante: nada apagado no passe completo --------------------------
def test_aplicar_preserva_todas_as_entradas():
    items = [
        _item(id=str(n), tipo="email_actionable", criado_em=HOJE.isoformat())
        for n in range(10)
    ] + [
        _item(id="ciclo1", tipo="fecho_contabilistico", empresa="OMNAI",
              metadata={"year_month": "2026-09"}),
    ]
    res = pr.aplicar(items, HOJE)
    assert len({d.item_id for d in res.decisoes}) == len(items)


# --- runner standalone (sem pytest) -----------------------------------------
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            print(f"ERRO  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(fns)} testes passaram")
    sys.exit(0 if ok == len(fns) else 1)
