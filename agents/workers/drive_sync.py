"""Worker: drive-sync | Ana | CFO. 11-09-2026.

Ate aqui a app so conhecia o que passava pelo arquivo automatico de email.
Uma factura que o David subia a mao para o Drive (a Moloni FR M/338493,
os PDFs da Hostinger, as fotos dos taloes) ficava invisivel para a tabela
`faturas`, para o pacote de fecho e para a resposta a contabilidade.

Este worker fecha esse buraco em dois sentidos:

  A. Pastas <Faturas OMNAI>/<Empresa>/<YYYY-Qn>/ : cada PDF que nao existe
     no arquivo em disco e descarregado, classificado (factura, recibo, nao
     factura), nomeado no padrao do arquivo e gravado. O ficheiro no Drive e
     renomeado para o mesmo nome (assim o upload automatico nao o duplica).
     Recibos vao para _recibos, no disco e no Drive.

  B. Pasta <Faturas OMNAI>/Inbox/ (e subpastas com nome de empresa): fotos
     (JPG, PNG, HEIC) e PDFs largados pelo David. As fotos passam pelo modelo
     com visao para tirar fornecedor, data, valor, NIF do destinatario; sao
     convertidas em PDF, arquivadas com nome canonico e subidas para
     <Empresa>/<Q>/. O original vai para Inbox/_processadas/.

No fim corre o indexador da tabela `faturas`. Documentos sem texto (fotos)
sao inscritos directamente na tabela com os metadados da visao, porque o
indexador so sabe ler nomes de ficheiro e texto de PDF.

Estado em /faturas/_drive_sync.json (id Drive -> caminho local). Reexecutavel.
Cron: de hora a hora, 07h a 22h. Tambem chamado pelo MCP (`drive_sync`).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

WORKER_NAME = "drive-sync"
FATURAS_ROOT = Path(os.getenv("FATURAS_DIR", "/faturas"))
ESTADO_FILE = FATURAS_ROOT / "_drive_sync.json"
EMPRESAS = ("OMNAI", "Previnsa", "JMSoares", "Sopato", "Pessoal")
RE_TRIMESTRE = re.compile(r"^\d{4}-Q[1-4]$")
IGNORAR_PASTAS = {"_recibos", "Fecho", "_processadas"}
MIME_IMAGEM = ("image/jpeg", "image/png", "image/heic", "image/heif", "image/webp")
EXT_IMAGEM = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")

SYSTEM_VISAO = """Es a assistente de contabilidade de uma pequena empresa portuguesa (OMNAI Consulting, Lda, NIF 519270592).
Recebes a fotografia de um documento (factura, factura-recibo, talao, recibo). Extrai em JSON puro (sem markdown):
{"e_documento_fiscal": true/false,
 "tipo": "fatura|fatura_recibo|recibo|talao_sem_nif|outro",
 "supplier": "nome curto do fornecedor" ou null,
 "supplier_nif": "NIF do fornecedor" ou null,
 "date": "YYYY-MM-DD" ou null,
 "amount": 12.34 ou null,
 "currency": "EUR",
 "invoice_number": "numero do documento" ou null,
 "customer_nif": "NIF do cliente impresso no documento" ou null,
 "customer_name": "nome do cliente impresso" ou null,
 "description": "o que foi comprado, curto" ou null,
 "legivel": true/false}
Regras: le com cuidado; datas portuguesas dd/mm/aaaa passam a ISO; virgula decimal passa a ponto; se algo nao estiver visivel, null; um talao sem NIF do cliente e "talao_sem_nif"."""


# ----------------------------------------------------------------- estado

def _estado() -> dict:
    try:
        return json.loads(ESTADO_FILE.read_text())
    except Exception:
        return {}


def _guardar_estado(e: dict) -> None:
    ESTADO_FILE.parent.mkdir(parents=True, exist_ok=True)
    ESTADO_FILE.write_text(json.dumps(e, indent=1))


def _slug(text: str, max_len: int = 40) -> str:
    from services.invoices import _slugify
    return _slugify(text or "", max_len)


def _data(v) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _valor(v) -> float | None:
    try:
        return float(str(v).replace(" ", "").replace(",", ".")) if v not in (None, "") else None
    except Exception:
        return None


def nome_canonico(meta: dict, recibo: bool = False) -> str:
    """Mesmo padrao do services.invoices: <fornecedor>_<data>_<valor><MOEDA>_<ref>.pdf"""
    d = _data(meta.get("date")) or date.today()
    partes = [_slug(meta.get("supplier") or "unknown"), d.isoformat()]
    v = _valor(meta.get("amount"))
    if v is not None:
        partes.append(f"{v:.2f}".replace(".", "-") + (meta.get("currency") or "EUR"))
    if meta.get("invoice_number"):
        partes.append(_slug(str(meta["invoice_number"]), 20))
    return "_".join(partes) + ("_recibo" if recibo else "") + ".pdf"


def _trimestre(meta: dict) -> str:
    from services.invoices import quarter_from_date
    return quarter_from_date(_data(meta.get("date")) or date.today())


def _nome_seguro(nome: str) -> str:
    """Nomes vindos do Drive podem ter '/' e ':' ("Digitalizado a 04/08/2026, 18:36:14.pdf")."""
    base = re.sub(r"[^\w.\- ]+", "-", nome).strip(" .-") or "documento.pdf"
    return base if base.lower().endswith(".pdf") else base + ".pdf"


def _gravar_local(rel_dir: Path, nome: str, dados: bytes) -> str:
    """Grava sem esmagar; devolve o caminho relativo. Regista o hash."""
    from services.invoices import _hashes, _registar_hash
    nome = _nome_seguro(nome)
    sha = hashlib.sha256(dados).hexdigest()
    ja = _hashes().get(sha)
    if ja and (FATURAS_ROOT / ja).exists():
        return ja
    destino = FATURAS_ROOT / rel_dir
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / nome
    i = 1
    while caminho.exists():
        caminho = destino / f"{Path(nome).stem}_{i}.pdf"
        i += 1
    caminho.write_bytes(dados)
    rel = str(caminho.relative_to(FATURAS_ROOT))
    _registar_hash(sha, rel)
    return rel


# ------------------------------------------------------------ tabela faturas

async def _inscrever(rel: str, empresa: str, trimestre: str, meta: dict,
                     estado: str = "por_validar", motivo: str | None = None,
                     aviso: str | None = None) -> str | None:
    """Linha na tabela faturas para documentos que o indexador nao sabe ler
    (fotos). Idempotente por ficheiro."""
    from services.briefing_db import _get_pool
    pool = await _get_pool()
    d = _data(meta.get("date"))
    valor = _valor(meta.get("amount"))
    async with pool.acquire() as conn:
        fid = await conn.fetchval(
            """INSERT INTO faturas (empresa, periodo, mes, fornecedor, data_fatura, valor, moeda,
                                    referencia, ficheiro, bytes, estado, motivo, aviso)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (ficheiro) DO NOTHING RETURNING id""",
            empresa, trimestre, date(d.year, d.month, 1) if d else None,
            (meta.get("supplier") or None), d, valor, (meta.get("currency") or "EUR"),
            (meta.get("invoice_number") or None), rel,
            os.path.getsize(FATURAS_ROOT / rel), estado, (motivo or "")[:500] or None,
            (aviso or "")[:300] or None)
    return str(fid) if fid else None


# ------------------------------------------------------------------ Drive

def _pastas_empresa(svc) -> list[tuple[str, str, str]]:
    """[(empresa, trimestre, folder_id)] para todas as <Empresa>/<YYYY-Qn> no Drive."""
    from services import drive
    cache = drive._load_cache()
    root = drive._ensure_folder(svc, drive.DRIVE_ROOT_NAME, None, cache)
    out = []
    for emp in EMPRESAS:
        emp_id = drive._find_folder(svc, emp, root)
        if not emp_id:
            continue
        r = svc.files().list(
            q=f"'{emp_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)", pageSize=100).execute()
        for f in r.get("files", []):
            if RE_TRIMESTRE.match(f["name"]):
                out.append((emp, f["name"], f["id"]))
    drive._save_cache(cache)
    return out


def _renomear(svc, file_id: str, nome: str) -> None:
    svc.files().update(fileId=file_id, body={"name": nome}).execute()


def _mover(svc, file_id: str, para: str, de: str, nome: str | None = None) -> None:
    body = {"name": nome} if nome else {}
    svc.files().update(fileId=file_id, body=body, addParents=para, removeParents=de).execute()


# --------------------------------------------------------- A. pastas Q

async def sincronizar_pastas(svc, estado: dict, limite: int = 40) -> dict:
    from services import drive
    from services.doc_fiscal import classificar, e_recibo, verificar_destinatario
    from services.invoices import extract_metadata, texto_pdf, _hashes

    conhecidos = set(estado.values())
    locais_por_nome = {p.name for p in FATURAS_ROOT.rglob("*.pdf")}
    stats = {"vistos": 0, "novos": 0, "renomeados": 0, "recibos": 0, "nao_faturas": 0,
             "sem_texto": 0, "erros": 0, "detalhe": []}
    novos_sem_texto: list[dict] = []

    for emp, trimestre, folder_id in _pastas_empresa(svc):
        for f in drive.list_folder(folder_id, svc=svc):
            if f.get("mimeType") != "application/pdf" or f["id"] in estado:
                continue
            stats["vistos"] += 1
            if f["name"] in locais_por_nome:
                # Ja existe localmente com o mesmo nome (subido pelo arquivo automatico).
                estado[f["id"]] = f"{emp}/{trimestre}/{f['name']}"
                continue
            if stats["novos"] >= limite:
                break
            try:
                dados = drive.download_bytes(f["id"], svc=svc)
                sha = hashlib.sha256(dados).hexdigest()
                ja = _hashes().get(sha)
                if ja and (FATURAS_ROOT / ja).exists():
                    # Conteudo ja arquivado com outro nome: alinha o nome no Drive.
                    nome_local = Path(ja).name
                    if f["name"] != nome_local:
                        _renomear(svc, f["id"], nome_local)
                        stats["renomeados"] += 1
                    estado[f["id"]] = ja
                    continue
                texto = texto_pdf(dados)
                if not texto.strip():
                    rel = _gravar_local(Path(emp) / trimestre, f["name"], dados)
                    estado[f["id"]] = rel
                    stats["sem_texto"] += 1
                    novos_sem_texto.append({"rel": rel, "empresa": emp, "trimestre": trimestre,
                                            "meta": {"supplier": Path(f["name"]).stem[:40]}})
                    continue
                veredicto = classificar(texto, f["name"])
                if not veredicto["e_fatura"]:
                    # Nao e despesa documentada (extrato, orcamento, comunicacao). Fica
                    # fora do arquivo contabilistico, e o Drive fica como esta.
                    from services.invoices import save_documento_triagem
                    meta = {"supplier": Path(f["name"]).stem[:40], "date": (f.get("modifiedTime") or "")[:10]}
                    r = save_documento_triagem(dados, meta, _inbox_para_empresa(emp), veredicto["tipo"],
                                               veredicto.get("natureza", ""))
                    estado[f["id"]] = str(Path(r["path"]).relative_to(FATURAS_ROOT))
                    stats["nao_faturas"] += 1
                    stats["detalhe"].append(f"{emp}/{trimestre}/{f['name']}: {veredicto['tipo']}")
                    continue
                meta = await extract_metadata(dados, hint_subject=f["name"])
                if meta.get("error") and not meta.get("supplier"):
                    meta = {"supplier": Path(f["name"]).stem[:40], "date": None}
                recibo = e_recibo(texto)["e_recibo"]
                nome = nome_canonico(meta, recibo)
                sub = Path(emp) / trimestre / ("_recibos" if recibo else "")
                rel = _gravar_local(sub, nome, dados)
                estado[f["id"]] = rel
                stats["novos"] += 1
                stats["recibos"] += int(recibo)
                if recibo:
                    rec_id = drive.ensure_invoice_folder(emp, trimestre, "_recibos")
                    _mover(svc, f["id"], rec_id, folder_id, Path(rel).name)
                elif f["name"] != Path(rel).name:
                    _renomear(svc, f["id"], Path(rel).name)
                    stats["renomeados"] += 1
                aviso = None if recibo else verificar_destinatario(texto, emp)
                stats["detalhe"].append(f"{emp}/{trimestre}/{f['name']} -> {Path(rel).name}"
                                        + (f" [aviso: {aviso}]" if aviso else ""))
            except Exception as exc:
                stats["erros"] += 1
                log.warning("drive_sync.pasta_falhou", ficheiro=f.get("name"), err=str(exc))
            _guardar_estado(estado)

    for n in novos_sem_texto:
        await _inscrever(n["rel"], n["empresa"], n["trimestre"], n["meta"],
                         aviso="PDF sem texto (digitalizado); confirmar dados a mao")
    return stats


def _inbox_para_empresa(emp: str) -> str:
    """save_documento_triagem recebe uma caixa de email; devolve uma que mapeie para a empresa."""
    from services.invoices import INBOX_TO_COMPANY
    for inbox, e in INBOX_TO_COMPANY.items():
        if e == emp:
            return inbox
    return "david.sardinha@omnai.pt"


# ------------------------------------------------------------ B. Inbox

def imagem_para_pdf(dados: bytes) -> bytes:
    """JPG/PNG/HEIC -> PDF de uma pagina. Precisa de Pillow (e pillow-heif para HEIC)."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    img = Image.open(io.BytesIO(dados))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # Limita a 2200 px no lado maior: chega para ler e nao pesa no Drive.
    img.thumbnail((2200, 2200))
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=150.0)
    return out.getvalue()


def _para_jpeg(dados: bytes, max_lado: int = 1600) -> tuple[bytes, str]:
    """Versao leve da imagem para mandar ao modelo."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(dados)))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_lado, max_lado))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue(), "image/jpeg"


async def ler_foto(dados: bytes) -> dict:
    from services.visao import generate_multimodal, json_da_resposta
    jpeg, mt = await asyncio.to_thread(_para_jpeg, dados)
    txt = await generate_multimodal(SYSTEM_VISAO, "Extrai os dados deste documento.", [(jpeg, mt)],
                                    max_tokens=600)
    try:
        meta = json_da_resposta(txt)
    except Exception:
        log.warning("drive_sync.visao_json_invalido", raw=txt[:200])
        meta = {"e_documento_fiscal": False, "legivel": False}
    for k in ("date",):
        v = meta.get(k)
        if v:
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
                try:
                    meta[k] = datetime.strptime(str(v).strip(), fmt).date().isoformat()
                    break
                except ValueError:
                    continue
    try:
        meta["amount"] = float(str(meta.get("amount")).replace(",", ".")) if meta.get("amount") not in (None, "") else None
    except Exception:
        meta["amount"] = None
    return meta


def _aviso_foto(meta: dict, empresa: str) -> str | None:
    from services.doc_fiscal import NIF_EMPRESA, NIF_PESSOAL
    nif = re.sub(r"\D", "", str(meta.get("customer_nif") or ""))
    esperado = NIF_EMPRESA.get(empresa)
    if meta.get("tipo") == "talao_sem_nif" or (not nif and esperado):
        return "talao/factura sem NIF do cliente; nao serve como despesa da empresa, pedir factura com NIF"
    if nif in NIF_PESSOAL:
        return f"documento em nome pessoal (NIF {nif}); pedir reemissao para {empresa} NIF {esperado}"
    if esperado and nif and nif != esperado:
        return f"NIF do cliente ({nif}) nao e o da {empresa} ({esperado})"
    if not meta.get("legivel", True):
        return "foto pouco legivel; confirmar dados a mao"
    return None


async def processar_inbox(svc, estado: dict, limite: int = 20) -> dict:
    from services import drive
    cache = drive._load_cache()
    root = drive._ensure_folder(svc, drive.DRIVE_ROOT_NAME, None, cache)
    # A pasta e criada se nao existir: e o sitio onde o David larga fotos.
    inbox = drive._ensure_folder(svc, "Inbox", root, cache)
    processadas = drive._ensure_folder(svc, "_processadas", inbox, cache)
    drive._save_cache(cache)
    stats = {"processados": 0, "erros": 0, "detalhe": []}

    # Inbox/ (OMNAI) e Inbox/<Empresa>/
    alvos = [("OMNAI", inbox)]
    r = svc.files().list(q=f"'{inbox}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                         fields="files(id,name)").execute()
    for f in r.get("files", []):
        if f["name"] in EMPRESAS:
            alvos.append((f["name"], f["id"]))

    for emp, pasta in alvos:
        for f in drive.list_folder(pasta, svc=svc):
            if stats["processados"] >= limite or f["id"] in estado:
                continue
            nome_l = f["name"].lower()
            mt = f.get("mimeType") or ""
            e_img = mt in MIME_IMAGEM or nome_l.endswith(EXT_IMAGEM)
            e_pdf = mt == "application/pdf" or nome_l.endswith(".pdf")
            if not (e_img or e_pdf):
                continue
            try:
                dados = drive.download_bytes(f["id"], svc=svc)
                if e_img:
                    meta = await ler_foto(dados)
                    pdf = await asyncio.to_thread(imagem_para_pdf, dados)
                    if not meta.get("e_documento_fiscal", False) and meta.get("tipo") in (None, "outro"):
                        rel = _gravar_local(Path("_nao_faturas") / "foto_nao_documento" / emp, f["name"] + ".pdf", pdf)
                        estado[f["id"]] = rel
                        _mover(svc, f["id"], processadas, pasta)
                        stats["detalhe"].append(f"{f['name']}: nao parece documento fiscal (fica em _nao_faturas)")
                        _guardar_estado(estado)
                        continue
                    recibo = meta.get("tipo") == "recibo"
                    trimestre = _trimestre(meta)
                    nome = nome_canonico(meta, recibo)
                    sub = Path(emp) / trimestre / ("_recibos" if recibo else "")
                    rel = _gravar_local(sub, nome, pdf)
                    estado[f["id"]] = rel
                    aviso = None if recibo else _aviso_foto(meta, emp)
                    if recibo:
                        motivo, est = "recibo de pagamento, nao e a factura", "ignorada"
                    else:
                        motivo, est = "foto lida por visao; dados por confirmar", "por_validar"
                    fid = await _inscrever(rel, emp, trimestre, meta, est, motivo, aviso)
                    drive.upload_bytes(pdf, Path(rel).name, emp, trimestre,
                                       subpasta="_recibos" if recibo else None)
                    _mover(svc, f["id"], processadas, pasta)
                    if aviso and fid:
                        from services.faturas_index import _card_aviso
                        await _card_aviso({"id": fid, "empresa": emp, "rel": rel,
                                           "fornecedor": meta.get("supplier"), "aviso": aviso})
                    stats["processados"] += 1
                    stats["detalhe"].append(f"{f['name']} -> {rel}" + (f" [aviso: {aviso}]" if aviso else ""))
                else:
                    # PDF largado no Inbox: mesmo caminho das pastas Q, e depois vai
                    # para <Empresa>/<Q>/ no Drive.
                    from services.doc_fiscal import classificar, e_recibo, verificar_destinatario
                    from services.invoices import extract_metadata, texto_pdf
                    texto = texto_pdf(dados)
                    if texto.strip():
                        meta = await extract_metadata(dados, hint_subject=f["name"])
                        recibo = e_recibo(texto)["e_recibo"]
                        e_fat = classificar(texto, f["name"])["e_fatura"] or recibo
                        aviso = None if recibo else verificar_destinatario(texto, emp)
                    else:
                        meta, recibo, e_fat, aviso = {"supplier": Path(f["name"]).stem[:40]}, False, True, \
                            "PDF sem texto (digitalizado); confirmar dados a mao"
                    if meta.get("error") and not meta.get("supplier"):
                        meta = {"supplier": Path(f["name"]).stem[:40]}
                    trimestre = _trimestre(meta)
                    if not e_fat:
                        rel = _gravar_local(Path("_nao_faturas") / "inbox" / emp / trimestre, f["name"], dados)
                        estado[f["id"]] = rel
                        _mover(svc, f["id"], processadas, pasta)
                        stats["detalhe"].append(f"{f['name']}: nao e factura (fica em _nao_faturas/inbox)")
                        _guardar_estado(estado)
                        continue
                    nome = nome_canonico(meta, recibo)
                    sub = Path(emp) / trimestre / ("_recibos" if recibo else "")
                    rel = _gravar_local(sub, nome, dados)
                    estado[f["id"]] = rel
                    if not texto.strip():
                        await _inscrever(rel, emp, trimestre, meta, aviso=aviso)
                    destino = drive.ensure_invoice_folder(emp, trimestre, "_recibos" if recibo else None)
                    _mover(svc, f["id"], destino, pasta, Path(rel).name)
                    stats["processados"] += 1
                    stats["detalhe"].append(f"{f['name']} -> {rel}" + (f" [aviso: {aviso}]" if aviso else ""))
            except Exception as exc:
                stats["erros"] += 1
                log.warning("drive_sync.inbox_falhou", ficheiro=f.get("name"), err=str(exc))
            _guardar_estado(estado)
    return stats


# ----------------------------------------------------------------- run

async def run(limite_pastas: int = 40, limite_inbox: int = 20) -> dict:
    from services import drive
    svc = await asyncio.to_thread(drive._service)
    estado = _estado()
    pastas = await sincronizar_pastas(svc, estado, limite_pastas)
    inbox = await processar_inbox(svc, estado, limite_inbox)
    _guardar_estado(estado)
    indice = {}
    try:
        from services.faturas_index import indexar
        indice = await indexar()
    except Exception as exc:
        log.warning("drive_sync.indexar_falhou", err=str(exc))
    out = {"status": "ok", "pastas": pastas, "inbox": inbox, "indice": indice}
    log.info("drive-sync", novos=pastas["novos"], inbox=inbox["processados"], erros=pastas["erros"] + inbox["erros"])
    return out
