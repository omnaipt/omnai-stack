"""Worker: resposta-contabilidade | Ana | CFO. 11-09-2026.

Quando a contabilidade (Eugest) escreve a pedir facturas em falta, movimentos
a esclarecer ou extratos, o pedido vem em texto e em imagens coladas no
email. Este worker:

  1. Procura na caixa OMNAI emails recentes de @eugest.pt ainda nao tratados.
  2. Le o texto, os PDFs anexos e as imagens (visao) e extrai uma lista
     estruturada de pedidos: {tipo, fornecedor, data, valor, moeda, referencia}.
  3. Verifica cada pedido contra o que a app ja tem, sem LLM: tabela faturas
     (validadas, por validar, recibos, avisos), movimentos Revolut
     (transferencias internas, socio), documentos Moloni.
  4. Cria um rascunho de resposta na thread, na caixa OMNAI, com o que esta
     resolvido (nome do ficheiro e link Drive) e com marcadores [A TRATAR]
     no que falta. Nunca envia.
  5. Emite um cartao no Hoje com o que falta.

Estado em /faturas/_estado/resposta_contabilidade.json (thread -> ultimo
message_id tratado). Reexecutavel: uma mensagem so e tratada uma vez.

Config opcional em /secrets/resposta_contabilidade.json:
  {"conta": "david.sardinha@omnai.pt", "dominios": ["eugest.pt"], "empresa": "OMNAI"}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

WORKER_NAME = "resposta-contabilidade"
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))
FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
ESTADO_FILE = FATURAS_ROOT / "_estado" / "resposta_contabilidade.json"
CONFIG_FILE = SECRETS_DIR / "resposta_contabilidade.json"

DEFAULT_CONFIG = {"conta": "david.sardinha@omnai.pt", "dominios": ["eugest.pt"],
                  "empresa": "OMNAI", "dias": 4}

TIPOS = ("fatura_em_falta", "movimento_a_esclarecer", "extrato", "documento_exterior",
         "pagamento_avenca", "outro")

SYSTEM = """Es a assistente de contabilidade de uma pequena empresa portuguesa (OMNAI Consulting, Lda, NIF 519270592).
Recebes um email da contabilista, com o texto e as imagens que ela colou (tabelas de facturas em falta no e-fatura, listas de movimentos bancarios, etc.).
Extrai TODOS os pedidos concretos, um por linha da tabela ou do texto, em JSON puro (sem markdown):
{"pedidos": [
  {"tipo": "fatura_em_falta|movimento_a_esclarecer|extrato|documento_exterior|pagamento_avenca|outro",
   "fornecedor": "nome tal como aparece" ou null,
   "data": "YYYY-MM-DD" ou null,
   "valor": 92.02 ou null,
   "moeda": "EUR",
   "referencia": "numero da factura/documento" ou null,
   "descricao": "frase curta com o que e pedido"}
 ],
 "periodo": "YYYY-MM" ou null,
 "prazo": "YYYY-MM-DD" ou null,
 "resumo": "uma frase com o que a contabilista quer"}
Regras: le as imagens com cuidado, linha a linha; datas em formato portugues (dd/mm/aaaa ou dd-mm-aaaa) passam a ISO; valores com virgula decimal passam a ponto; nao inventes linhas; se uma tabela tiver 6 linhas, devolve 6 pedidos."""


def config() -> dict:
    try:
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
    except FileNotFoundError:
        return DEFAULT_CONFIG
    except Exception as exc:
        log.warning("resposta_contabilidade.config_invalida", err=str(exc))
        return DEFAULT_CONFIG


def _estado() -> dict:
    try:
        return json.loads(ESTADO_FILE.read_text())
    except Exception:
        return {}


def _guardar_estado(e: dict) -> None:
    ESTADO_FILE.parent.mkdir(parents=True, exist_ok=True)
    ESTADO_FILE.write_text(json.dumps(e, indent=1))


# ---------------------------------------------------------------- leitura

def _ler_mensagem(conta: str, message_id: str) -> dict:
    """Texto, PDFs (texto) e imagens (bytes) de uma mensagem."""
    from mcp_server import _extract_body_full, _pdf_text
    from services import gmail
    svc = gmail._service(conta)
    m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    corpo = _extract_body_full(m, 20000)
    imagens: list[tuple[bytes, str]] = []
    pdfs: list[str] = []
    for att in gmail.list_attachments(m):
        mt = (att.get("mime_type") or "").lower()
        nome = (att.get("filename") or "").lower()
        try:
            dados = gmail.download_attachment(conta, message_id, att["attachment_id"])
        except Exception as exc:
            log.warning("resposta_contabilidade.anexo_falhou", nome=nome, err=str(exc))
            continue
        if mt.startswith("image/") or nome.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            mt = mt if mt.startswith("image/") else "image/" + ("jpeg" if nome.endswith((".jpg", ".jpeg")) else nome.rsplit(".", 1)[-1])
            imagens.append((dados, mt))
        elif nome.endswith(".pdf") or dados[:4] == b"%PDF":
            pdfs.append(f"[anexo {att.get('filename')}]\n" + _pdf_text(dados, 8000))
    return {
        "id": m.get("id"), "thread_id": m.get("threadId"),
        "from": gmail._header(m, "From"), "subject": gmail._header(m, "Subject"),
        "date": gmail._header(m, "Date"), "message_id_header": gmail._header(m, "Message-ID"),
        "corpo": corpo, "imagens": imagens, "pdfs": pdfs,
    }


async def extrair_pedidos(msg: dict) -> dict:
    from services.visao import generate_multimodal, json_da_resposta
    prompt = (f"De: {msg['from']}\nAssunto: {msg['subject']}\nData: {msg['date']}\n\n"
              f"Texto do email:\n{msg['corpo'][:12000]}\n\n"
              + ("\n\n".join(msg["pdfs"])[:12000] if msg["pdfs"] else "")
              + f"\n\nHa {len(msg['imagens'])} imagem(ns) coladas no email, acima. Extrai os pedidos.")
    txt = await generate_multimodal(SYSTEM, prompt, msg["imagens"], max_tokens=3000)
    try:
        out = json_da_resposta(txt)
    except Exception:
        log.warning("resposta_contabilidade.json_invalido", raw=txt[:300])
        out = {"pedidos": [], "resumo": txt[:300]}
    pedidos = []
    for p in out.get("pedidos", []) or []:
        if not isinstance(p, dict):
            continue
        p["tipo"] = p.get("tipo") if p.get("tipo") in TIPOS else "outro"
        try:
            p["valor"] = float(str(p.get("valor")).replace(",", ".")) if p.get("valor") not in (None, "") else None
        except Exception:
            p["valor"] = None
        p["data"] = _data_iso(p.get("data"))
        pedidos.append(p)
    out["pedidos"] = pedidos
    return out


def _data_iso(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- verificacao

def _tokens(s: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 3}


def _perto(a: float | None, b, tol: float = 0.011) -> bool:
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) <= tol
    except Exception:
        return False


def _dias(a: str | None, b) -> int | None:
    try:
        d1 = date.fromisoformat(a) if isinstance(a, str) else a
        d2 = date.fromisoformat(b) if isinstance(b, str) else b
        return abs((d1 - d2).days)
    except Exception:
        return None


async def verificar(pedidos: list[dict], empresa: str, periodo: str | None) -> list[dict]:
    """Junta a cada pedido o que a app sabe. Deterministico."""
    from services import faturas_db
    meses = set()
    for p in pedidos:
        if p.get("data"):
            meses.add(p["data"][:7])
    for m in re.findall(r"\d{4}-\d{2}", periodo or ""):
        meses.add(m)
    if not meses:
        hoje = date.today()
        meses = {f"{hoje.year}-{hoje.month:02d}", (date(hoje.year, hoje.month, 1) - timedelta(days=1)).strftime("%Y-%m")}

    faturas: list[dict] = []
    for m in sorted(meses):
        faturas += await faturas_db.listar(mes=m, empresa=empresa, estado="todos", limit=500)
    movimentos: list[dict] = []
    try:
        from services import movimentos_db
        for m in sorted(meses):
            movimentos += await movimentos_db.listar(mes=m, limit=2000)
    except Exception as exc:
        log.warning("resposta_contabilidade.movimentos_indisponiveis", err=str(exc))
    moloni_docs: list[dict] = []
    try:
        from services import moloni
        if moloni.configured():
            for m in sorted(meses):
                moloni_docs += await asyncio.to_thread(moloni.documents, m)
    except Exception as exc:
        log.warning("resposta_contabilidade.moloni_indisponivel", err=str(exc))

    drive_links = await asyncio.to_thread(_links_drive, [f["ficheiro"] for f in faturas
                                                         if f.get("estado") in ("validada", "entregue")])

    for p in pedidos:
        p["verificacao"] = _verificar_um(p, faturas, movimentos, moloni_docs, drive_links)
    return pedidos


def _links_drive(ficheiros: list[str]) -> dict[str, str]:
    """Nome de ficheiro -> link Drive, procurando pelo nome na pasta da empresa."""
    out: dict[str, str] = {}
    if not ficheiros:
        return out
    try:
        from services import drive
        svc = drive._service()
        nomes = {Path(f).name for f in ficheiros}
        # Uma pesquisa por nome de cada vez e lento; a Drive aceita OR.
        lote = list(nomes)
        for i in range(0, len(lote), 20):
            q = " or ".join("name='%s'" % n.replace("'", "\\'") for n in lote[i:i + 20])
            r = svc.files().list(q=f"({q}) and trashed=false", spaces="drive",
                                 fields="files(id,name,webViewLink)", pageSize=100).execute()
            for f in r.get("files", []):
                out.setdefault(f["name"], f.get("webViewLink"))
    except Exception as exc:
        log.warning("resposta_contabilidade.drive_links_falhou", err=str(exc))
    return out


def _verificar_um(p: dict, faturas: list[dict], movimentos: list[dict],
                  moloni_docs: list[dict], links: dict[str, str]) -> dict:
    toks = _tokens(p.get("fornecedor")) | _tokens(p.get("descricao"))
    ref = (p.get("referencia") or "").lower().replace(" ", "")
    v: dict[str, Any] = {"estado": "por_tratar", "faturas": [], "movimentos": [], "nota": None}

    # 1. facturas no arquivo
    cands = []
    for f in faturas:
        pontos = 0.0
        if ref and f.get("referencia") and ref.replace("/", "-") in (f["referencia"] or "").lower().replace(" ", ""):
            pontos += 3
        if _perto(p.get("valor"), f.get("valor")):
            pontos += 2
        ft = _tokens(f.get("fornecedor")) | _tokens(f.get("ficheiro"))
        if toks & ft:
            pontos += 1.5
        d = _dias(p.get("data"), f.get("data_fatura"))
        if d is not None and d <= 3:
            pontos += 1
        elif d is not None and d <= 31 and pontos >= 2:
            pontos += 0.3
        if pontos >= 3:
            cands.append((pontos, f))
    cands.sort(key=lambda x: -x[0])
    for pontos, f in cands[:3]:
        nome = Path(f["ficheiro"]).name
        v["faturas"].append({"ficheiro": nome, "estado": f["estado"], "aviso": f.get("aviso"),
                             "motivo": f.get("motivo"), "link": links.get(nome), "pontos": pontos,
                             "fatura_id": str(f["id"])})

    # 2. movimentos bancarios
    for m in movimentos:
        pontos = 0.0
        if _perto(p.get("valor"), abs(float(m["valor"]))) or (m.get("valor_orig") and _perto(p.get("valor"), abs(float(m["valor_orig"])))):
            pontos += 2
        d = _dias(p.get("data"), m["data"])
        if d is not None and d <= 3:
            pontos += 1
        if toks & (_tokens(m.get("contraparte")) | _tokens(m.get("descricao"))):
            pontos += 1
        if pontos >= 3:
            v["movimentos"].append({"data": str(m["data"]), "valor": float(m["valor"]), "moeda": m["moeda"],
                                    "categoria": m["categoria"], "match_estado": m["match_estado"],
                                    "descricao": m.get("descricao"), "nota": m.get("nota"),
                                    "fatura_ficheiro": m.get("fatura_ficheiro"), "conta": m.get("conta_nome")})

    # 3. decisao
    if v["faturas"] and v["faturas"][0]["estado"] in ("validada", "entregue") and not v["faturas"][0].get("aviso"):
        v["estado"] = "resolvido"
        v["nota"] = f"factura no arquivo: {v['faturas'][0]['ficheiro']}"
    elif v["faturas"] and v["faturas"][0].get("aviso"):
        v["estado"] = "com_aviso"
        v["nota"] = f"factura existe mas {v['faturas'][0]['aviso']}"
    elif v["faturas"] and v["faturas"][0]["estado"] == "por_validar":
        v["estado"] = "por_validar"
        v["nota"] = f"factura no arquivo por validar na app: {v['faturas'][0]['ficheiro']}"
    elif v["faturas"] and v["faturas"][0]["estado"] == "ignorada" and "recibo" in (v["faturas"][0].get("motivo") or ""):
        v["estado"] = "so_recibo"
        v["nota"] = "so existe o recibo de pagamento; falta a factura"
    elif v["movimentos"] and v["movimentos"][0]["categoria"] == "interno":
        v["estado"] = "resolvido"
        v["nota"] = "transferencia interna entre contas da empresa (conta a ordem e Poupanca Omnai); nao e despesa nem receita"
    elif v["movimentos"] and v["movimentos"][0]["categoria"] in ("juros", "taxa", "cambio"):
        cat = v["movimentos"][0]["categoria"]
        v["estado"] = "resolvido"
        v["nota"] = {"juros": "juros da conta poupanca Revolut (extrato)", "taxa": "comissao Revolut (extrato)",
                     "cambio": "cambio entre contas Revolut (extrato)"}[cat]
    elif v["movimentos"] and (v["movimentos"][0].get("nota") or "").startswith("socio"):
        v["estado"] = "por_tratar"
        v["nota"] = "transferencia para o socio; precisa de mapa de despesas ou de km"
    elif v["movimentos"] and v["movimentos"][0].get("fatura_ficheiro"):
        v["estado"] = "resolvido"
        v["nota"] = f"movimento casado com a factura {v['movimentos'][0]['fatura_ficheiro']}"
    elif p.get("tipo") == "extrato":
        v["estado"] = "por_tratar"
        v["nota"] = "extrato: descarregar o PDF na app do banco (a API do Revolut nao o fornece)"
    for doc in moloni_docs:
        if ref and ref.replace("/", "-") in (doc.get("numero") or "").lower().replace("/", "-"):
            v["estado"] = "resolvido"
            v["nota"] = f"documento emitido na Moloni: {doc['numero']} ({doc['data']})"
    return v


# ---------------------------------------------------------------- rascunho

def _fmt(v) -> str:
    try:
        return f"{float(v):.2f}".replace(".", ",")
    except Exception:
        return str(v)


NOTAS_POR_TIPO = {
    "pagamento_avenca": "confirmar o pagamento da avença e enviar o comprovativo",
    "extrato": "descarregar o PDF do extrato na app do banco e anexar",
    "documento_exterior": "obter o documento e anexar",
    "outro": None,
}


def texto_resposta(extr: dict, pedidos: list[dict], nome_remetente: str | None) -> str:
    cfg_nomes = config().get("saudacao")
    saudacao = cfg_nomes or (f"Olá {nome_remetente}," if nome_remetente else "Bom dia,")
    L = [saudacao, "",
         "Segue ponto a ponto o que pediram. Os ficheiros estão na pasta Drive partilhada (Faturas OMNAI) e vão também em anexo.", ""]
    n = 0
    for p in pedidos:
        n += 1
        v = p["verificacao"]
        cab = " ".join(x for x in [
            p.get("data") and datetime.fromisoformat(p["data"]).strftime("%d/%m"),
            p.get("fornecedor"), p.get("referencia"),
            f"{_fmt(p['valor'])} {p.get('moeda') or 'EUR'}" if p.get("valor") is not None else None,
        ] if x) or (p.get("descricao") or "pedido")
        if v["estado"] == "resolvido":
            linha = v["nota"]
            if v["faturas"] and v["faturas"][0].get("link"):
                linha += f" ({v['faturas'][0]['link']})"
            L.append(f"{n}. {cab}: {linha}.")
        elif v["estado"] == "com_aviso":
            L.append(f"{n}. {cab}: [A TRATAR] {v['nota']}. Já pedi a reemissão ao fornecedor; envio a versão corrigida assim que a receber.")
        elif v["estado"] == "por_validar":
            L.append(f"{n}. {cab}: [A TRATAR] {v['nota']}.")
        elif v["estado"] == "so_recibo":
            L.append(f"{n}. {cab}: [A TRATAR] {v['nota']}; vou pedir ao fornecedor.")
        else:
            nota = v.get("nota") or NOTAS_POR_TIPO.get(p.get("tipo")) or "sem documento na app; vou obter"
            L.append(f"{n}. {cab}: [A TRATAR] {nota}.")
    L += ["", "Qualquer coisa que falte, digam.", "", "Cumprimentos,", "David Sardinha"]
    return "\n".join(L)


GENERICOS = {"geral", "info", "contacto", "contact", "mail", "email", "admin", "suporte", "support"}


def _nome_remetente(from_addr: str) -> str | None:
    """Primeiro nome do remetente, ou None se for uma caixa generica ("Geral Eugest")."""
    nome = re.sub(r"<.*?>", "", from_addr or "").strip().strip('"')
    if not nome:
        return None
    primeiro = nome.split()[0]
    return None if primeiro.lower() in GENERICOS else primeiro


def _endereco(from_addr: str) -> str:
    m = re.search(r"<([^>]+)>", from_addr or "")
    return (m.group(1) if m else (from_addr or "")).strip()


# ---------------------------------------------------------------- pipeline

async def tratar_mensagem(conta: str, message_id: str, empresa: str, criar_rascunho: bool = True) -> dict:
    from services import gmail
    msg = await asyncio.to_thread(_ler_mensagem, conta, message_id)
    extr = await extrair_pedidos(msg)
    pedidos = await verificar(extr["pedidos"], empresa, extr.get("periodo"))
    faltam = [p for p in pedidos if p["verificacao"]["estado"] != "resolvido"]
    corpo = texto_resposta(extr, pedidos, _nome_remetente(msg["from"]))
    out: dict[str, Any] = {
        "message_id": message_id, "thread_id": msg["thread_id"], "subject": msg["subject"],
        "imagens": len(msg["imagens"]), "pedidos": len(pedidos), "resolvidos": len(pedidos) - len(faltam),
        "por_tratar": [{"descricao": p.get("descricao"), "fornecedor": p.get("fornecedor"),
                        "valor": p.get("valor"), "data": p.get("data"),
                        "estado": p["verificacao"]["estado"], "nota": p["verificacao"]["nota"]} for p in faltam],
        "resumo": extr.get("resumo"), "prazo": extr.get("prazo"), "rascunho": corpo,
    }
    if criar_rascunho:
        assunto = msg["subject"] if msg["subject"].lower().startswith("re:") else f"Re: {msg['subject']}"
        try:
            d = await asyncio.to_thread(gmail.create_draft, conta, _endereco(msg["from"]), assunto, corpo,
                                        msg["thread_id"], msg["message_id_header"])
            out["draft_id"] = d.get("id")
        except Exception as exc:
            out["draft_erro"] = f"{type(exc).__name__}: {exc}"
    if not criar_rascunho:
        return out  # analise a seco: sem cartao
    try:
        from services.briefing_emit import emit_briefing
        await emit_briefing(
            tipo="resposta_contabilidade",
            titulo=f"Contabilidade pede {len(pedidos)} coisa(s): {len(faltam)} por tratar, rascunho pronto"
                   if faltam else f"Contabilidade: {len(pedidos)} pedido(s) todos cobertos, rascunho pronto",
            detalhe=(f"Assunto: {msg['subject']}\nPrazo: {extr.get('prazo') or '-'}\n\n"
                     + "\n".join(f"- {p['fornecedor'] or ''} {p['data'] or ''} {_fmt(p['valor']) if p['valor'] is not None else ''}: {p['nota'] or p['estado']}"
                                 for p in out["por_tratar"]))[:1800],
            urgencia="P1" if faltam else "P2", empresa=empresa,
            chave_parts=(msg["thread_id"],),  # um cartao por conversa
            link_origem=gmail.get_message_url(conta, message_id),
            metadata={"thread_id": msg["thread_id"], "draft_id": out.get("draft_id"), "prazo": extr.get("prazo")},
            worker_name=WORKER_NAME)
    except Exception as exc:
        log.warning("resposta_contabilidade.card_falhou", err=str(exc))
    return out


def _listar_novas(conta: str, dominios: list[str], dias: int) -> list[dict]:
    from services import gmail
    svc = gmail._service(conta)
    q = "(" + " OR ".join(f"from:@{d}" for d in dominios) + f") newer_than:{dias}d -from:me"
    r = svc.users().messages().list(userId="me", q=q, maxResults=20).execute()
    return r.get("messages", []) or []


async def run(criar_rascunho: bool = True) -> dict:
    cfg = config()
    estado = _estado()
    novas = await asyncio.to_thread(_listar_novas, cfg["conta"], cfg["dominios"], int(cfg.get("dias", 4)))
    tratadas, saltadas = [], 0
    for item in novas:
        tid, mid = item.get("threadId"), item["id"]
        if estado.get(tid, {}).get("ultimo_message_id") == mid or mid in estado.get(tid, {}).get("tratadas", []):
            saltadas += 1
            continue
        try:
            r = await tratar_mensagem(cfg["conta"], mid, cfg["empresa"], criar_rascunho)
            tratadas.append({k: v for k, v in r.items() if k != "rascunho"} if criar_rascunho else r)
            if not criar_rascunho:
                continue  # analise a seco: nao conta como tratada
            e = estado.setdefault(tid, {"tratadas": []})
            e["ultimo_message_id"] = mid
            e.setdefault("tratadas", []).append(mid)
            e["draft_id"] = r.get("draft_id")
            e["quando"] = datetime.now().isoformat(timespec="seconds")
            _guardar_estado(estado)
        except Exception as exc:
            log.warning("resposta_contabilidade.falhou", message_id=mid, err=str(exc))
            tratadas.append({"message_id": mid, "erro": f"{type(exc).__name__}: {exc}"})
    return {"status": "ok", "novas": len(novas), "tratadas": tratadas, "saltadas": saltadas}
