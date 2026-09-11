# -*- coding: utf-8 -*-
"""Mantem a tabela faturas a par do que esta em disco.

03-08-2026. Descoberto por acidente: as 60 linhas da tabela tinham TODAS a
mesma data de criacao, 31/07 15:41, que foi quando corri o backfill a mao.
Nao existia indexador nenhum a correr. O worker arquivava os PDFs em disco e
mais ninguem os registava, por isso o ecra Faturas mostrava uma fotografia
de 31 de Julho e o David nao tinha como saber. E a mesma falha silenciosa do
Drive e das accoes do Hoje, pela terceira vez neste sistema.

Este modulo e idempotente: pode correr as vezes que forem precisas. Uma
factura que o David ja validou ou entregou nao volta atras, so se
actualizam as que ainda nao tem decisao dele.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime

EMPRESAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal", "Outros")

RAIZ = os.getenv("FATURAS_DIR", "/faturas")

# <fornecedor>_<AAAA-MM-DD>_<valor><MOEDA>_<referencia>[_<n>].pdf
RE_NOME = re.compile(
    r"^(?P<fornecedor>.+?)_(?P<data>\d{4}-\d{2}-\d{2})"
    r"(?:_(?P<valor>[\d-]+)(?P<moeda>EUR|USD|GBP))?"
    r"(?:_(?P<ref>.+?))?(?:_(?P<copia>\d+))?\.pdf$", re.I)


def _partes(rel: str) -> tuple[str, str, str | None]:
    """Devolve (empresa, trimestre, motivo_da_pasta) a partir do caminho."""
    p = rel.replace("\\", "/").split("/")
    motivo = None
    # 11-09-2026: recibos de pagamento ficam em <Empresa>/<Q>/_recibos e
    # nao entram no pacote. Ficam na tabela como ignorados, com o motivo.
    if "_recibos" in p:
        p = [x for x in p if x != "_recibos"]
        return (next((x for x in p if x in EMPRESAS), "Outros"),
                next((x for x in p if re.match(r"^\d{4}-Q[1-4]$", x)), ""),
                "recibo")
    if p and p[0].startswith("_"):
        motivo = p[1] if p[0] == "_nao_faturas" and len(p) > 1 else p[0].lstrip("_")
        p = p[2:] if p[0] == "_nao_faturas" else p[1:]
    empresa = next((x for x in p if x in EMPRESAS), "Outros")
    trimestre = next((x for x in p if re.match(r"^\d{4}-Q[1-4]$", x)), "")
    return empresa, trimestre, motivo


def ler_nome(nome: str) -> dict:
    m = RE_NOME.match(nome)
    if not m:
        return {"fornecedor": None, "data": None, "valor": None,
                "moeda": None, "ref": None}
    g = m.groupdict()
    valor = None
    if g.get("valor"):
        try:
            valor = float(g["valor"].replace("-", "."))
        except ValueError:
            valor = None
    data = None
    try:
        data = datetime.strptime(g["data"], "%Y-%m-%d").date()
    except Exception:
        pass
    return {
        "fornecedor": (g.get("fornecedor") or "").replace("-", " ").strip() or None,
        "data": data, "valor": valor,
        "moeda": (g.get("moeda") or "EUR").upper(),
        "ref": g.get("ref"),
    }


def _texto_pdf(caminho: str, paginas: int = 4) -> str:
    from pypdf import PdfReader
    try:
        r = PdfReader(caminho)
        return "\n".join((p.extract_text() or "") for p in r.pages[:paginas])
    except Exception:
        return ""


async def indexar(classificar_novos: bool = True) -> dict:
    """Garante uma linha por PDF. Devolve contagens."""
    from services.briefing_db import _get_pool
    from services.doc_fiscal import classificar, e_recibo, verificar_destinatario

    pool = await _get_pool()
    novos, actualizados, intactos = 0, 0, 0
    avisos: list[dict] = []

    for pasta, _, ficheiros in os.walk(RAIZ):
        for nome in sorted(ficheiros):
            if not nome.lower().endswith(".pdf"):
                continue
            caminho = os.path.join(pasta, nome)
            rel = os.path.relpath(caminho, RAIZ)
            empresa, trimestre, motivo_pasta = _partes(rel)
            info = ler_nome(nome)

            async with pool.acquire() as conn:
                existente = await conn.fetchrow(
                    "SELECT id, estado FROM faturas WHERE ficheiro = $1", rel)
                if existente:
                    # O que o David ja decidiu nao se mexe.
                    intactos += 1
                    continue

                estado, motivo, aviso = "por_validar", None, None
                if motivo_pasta:
                    estado = "ignorada"
                    motivo = "arquivado em %s" % motivo_pasta
                elif classificar_novos:
                    texto = _texto_pdf(caminho)
                    r = classificar(texto, nome)
                    if not r["e_fatura"]:
                        estado = "ignorada"
                        motivo = "%s: %s" % (r["tipo"], r["porque"])
                    elif e_recibo(texto)["e_recibo"]:
                        # Chegou antes desta versao, ou por outra via: e um
                        # recibo no meio das facturas. Nao se move o ficheiro
                        # daqui (o David pode discordar), mas nao entra no pacote.
                        estado = "ignorada"
                        motivo = "recibo de pagamento, nao e a factura"
                    else:
                        aviso = verificar_destinatario(texto, empresa)

                mes = date(info["data"].year, info["data"].month, 1) \
                    if info["data"] else None
                fid = await conn.fetchval(
                    """INSERT INTO faturas
                         (empresa, periodo, mes, fornecedor, data_fatura, valor,
                          moeda, referencia, ficheiro, bytes, estado, motivo, aviso)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                       ON CONFLICT (ficheiro) DO NOTHING
                       RETURNING id""",
                    empresa, trimestre or None, mes, info["fornecedor"],
                    info["data"], info["valor"], info["moeda"], info["ref"],
                    rel, os.path.getsize(caminho), estado,
                    (motivo or "")[:500] or None, (aviso or "")[:300] or None)
                novos += 1
                if aviso and fid:
                    avisos.append({"id": str(fid), "empresa": empresa, "rel": rel,
                                   "fornecedor": info["fornecedor"], "aviso": aviso})

    for a in avisos:
        await _card_aviso(a)

    return {"novos": novos, "ja_existiam": intactos, "actualizados": actualizados,
            "avisos": len(avisos)}


def log_aviso(evento: str, exc: Exception) -> None:
    import structlog
    structlog.get_logger().warning(evento, err=str(exc))


async def _card_aviso(a: dict) -> None:
    """Factura em nome pessoal ou com NIF errado: cartao P1 no Hoje, para o
    David pedir a reemissao enquanto o fornecedor ainda a corrige."""
    try:
        from services.briefing_emit import emit_briefing
        await emit_briefing(
            tipo="fatura_destinatario",
            titulo=f"Factura {a['fornecedor'] or '?'}: {a['aviso'][:120]}",
            detalhe=f"Ficheiro: {a['rel']}\nEmpresa: {a['empresa']}\n{a['aviso']}\n"
                    "Accao: pedir ao fornecedor a reemissao com o NIF certo antes do fecho.",
            urgencia="P1", empresa=a["empresa"], chave_parts=(a["id"],),
            metadata={"fatura_id": a["id"], "aviso": a["aviso"]},
            worker_name="faturas-index")
    except Exception as exc:  # nunca parar o indice por causa do cartao
        import structlog
        structlog.get_logger().warning("faturas.card_aviso_falhou", err=str(exc))


async def rever_existentes(desde: str = "2026-07") -> dict:
    """11-09-2026: passa as regras novas (recibo, destinatario) pelas linhas
    que ja existiam. O que o David validou nao muda de estado: se parecer
    recibo ou estiver em nome errado, fica com aviso para ele ver. O que
    ainda esta por validar e for recibo passa a ignorado."""
    from services.briefing_db import _get_pool
    from services.doc_fiscal import e_recibo, verificar_destinatario

    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, empresa, ficheiro, estado, fornecedor, aviso FROM faturas
                WHERE mes >= ($1 || '-01')::date AND estado IN ('por_validar', 'validada')""", desde)
    recibos, avisos = 0, 0
    novos_avisos = []
    for r in rows:
        caminho = os.path.join(RAIZ, r["ficheiro"])
        if not os.path.isfile(caminho):
            continue
        texto = _texto_pdf(caminho)
        aviso = None
        if e_recibo(texto)["e_recibo"]:
            recibos += 1
            if r["estado"] == "por_validar":
                async with pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE faturas SET estado='ignorada',
                                  motivo='recibo de pagamento, nao e a factura',
                                  actualizado_em=now() WHERE id=$1""", r["id"])
                continue
            aviso = "parece recibo de pagamento, nao a factura"
        else:
            aviso = verificar_destinatario(texto, r["empresa"])
        antigo = r["aviso"] or ""
        nosso = antigo.startswith(("NIF errado", "factura ", "parece recibo"))
        if aviso and aviso != antigo:
            avisos += 1
            async with pool.acquire() as conn:
                await conn.execute("UPDATE faturas SET aviso=$2, actualizado_em=now() WHERE id=$1",
                                   r["id"], aviso[:300])
            novos_avisos.append({"id": str(r["id"]), "empresa": r["empresa"], "rel": r["ficheiro"],
                                 "fornecedor": r["fornecedor"], "aviso": aviso})
        elif not aviso and nosso:
            # A regra mudou e o aviso antigo era nosso: limpa-o e fecha o cartao.
            async with pool.acquire() as conn:
                await conn.execute("UPDATE faturas SET aviso=NULL, actualizado_em=now() WHERE id=$1", r["id"])
            try:
                from services.briefing_db import make_chave, mark_done_by_chave
                await mark_done_by_chave(make_chave("fatura_destinatario", str(r["id"])))
            except Exception as exc:
                log_aviso("faturas.card_fechar_falhou", exc)
    for a in novos_avisos:
        await _card_aviso(a)
    return {"revistas": len(rows), "recibos": recibos, "avisos": avisos}
