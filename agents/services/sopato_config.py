"""
sopato_config.py

Configuracao central do dominio Sopato (gestao do predio Cascais).
Antes de Sprint 4 esta lista de inquilinos era hardcoded; agora e carregada
da database Notion "Inquilinos Sopato" com cache em memoria (TTL 1h) e
fallback para data/inquilinos_cache_seed.json se Notion estiver indisponivel.

Pontos de extensao:
    - INQUILINOS_SEM_FATURA: whitelist explicita para inquilinos cujo
      pagamento e em dinheiro e nao gera fatura (ex: Ricardo Rocha).
      verificacao_recibos_sopato consome esta lista para fazer skip do
      check de Recibo Emitido.

CORRECAO Sprint 4.2:
    - Imports alterados de `agents.services.*` para `services.*` para
      alinhar com os imports usados em main.py e workers/briefing_inbox.py
      (ver omnai-agents-current/main.py:14-32 e briefing_inbox.py:17-22).
    - Funcao `listar_inquilinos_activos()` exposta como wrapper sincrono
      sobre `get_cached_inquilinos()` para preservar API esperada pelos
      workers actuais (ver verificacao_recibos_sopato.py actual no VPS,
      linha 69: `from services.sopato_config import listar_inquilinos_activos`).
    - `__getattr__` adicionado para servir `INQUILINOS` legacy a partir
      da cache, devolvendo lista vazia se ainda nao foi carregada.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from services.notion import NotionClient
from services.notion_inquilinos import fetch_inquilinos_raw

log = logging.getLogger(__name__)

# Inquilinos cujo pagamento e em dinheiro e nao emitem fatura.
# verificacao_recibos_sopato faz skip destes nomes.
INQUILINOS_SEM_FATURA: set[str] = {"Ricardo Rocha"}

# Status considerados activos para efeitos de processamento.
STATUS_ACTIVOS: set[str] = {"Activo", "Em saida"}

# Cache em memoria.
_CACHE_TTL_SECONDS = 3600
_cache_lock = asyncio.Lock()
_cache: dict[str, Any] = {
    "loaded_at": 0.0,
    "inquilinos": [],
}

# Localizacao do seed de fallback. Container OMNAI monta agents/->/app,
# WORKDIR e /app, nao ha /app/data por defeito. Manter o seed JUNTO ao
# codigo em services/ que esta sempre visivel via mount existente.
_SEED_PATH_DEFAULT = Path(__file__).resolve().parent / "inquilinos_cache_seed.json"
SEED_PATH = Path(os.environ.get("SOPATO_INQUILINOS_SEED_PATH", str(_SEED_PATH_DEFAULT)))


@dataclass
class Inquilino:
    """
    Representa um inquilino do predio Sopato.
    Mapeia 1:1 com o schema da database Notion "Inquilinos Sopato".
    Campos opcionais ficam None se nao preenchidos.
    """
    nome: str
    fraccao: str | None = None
    email: str | None = None
    telefone: str | None = None
    renda_mensal: float | None = None
    inicio_contrato: str | None = None  # ISO date string
    fim_contrato: str | None = None     # ISO date string
    status: str | None = None
    notas: str | None = None
    notion_id: str | None = None
    sem_fatura: bool = field(default=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # Compatibilidade com codigo legacy que ainda acede a chaves estilo dict
    # (ex: inq["id"], inq["nome"], inq["fraccao"]).
    @property
    def id(self) -> str:
        if self.notion_id:
            return self.notion_id
        # slug pessimista a partir do nome para keys estaveis
        return self.nome.lower().replace(" ", "-")


def _coerce_inquilino(raw: dict[str, Any]) -> Inquilino | None:
    """
    Valida com warnings (nao excepcoes) e devolve Inquilino ou None se invalido.
    """
    nome = raw.get("nome")
    if not nome or not isinstance(nome, str):
        log.warning("inquilino invalido sem nome: %s", raw)
        return None

    renda = raw.get("renda_mensal")
    if renda is not None and not isinstance(renda, (int, float)):
        log.warning("renda_mensal nao numerica para %s, normalizando para None", nome)
        renda = None

    sem_fatura = nome in INQUILINOS_SEM_FATURA
    notas = raw.get("notas") or ""
    if not sem_fatura and "sem fatura" in notas.lower():
        # Heuristica auxiliar: se a nota explicita "sem fatura", honrar.
        log.info("inquilino %s marcado sem_fatura por notas", nome)
        sem_fatura = True

    return Inquilino(
        nome=nome,
        fraccao=raw.get("fraccao"),
        email=raw.get("email"),
        telefone=raw.get("telefone"),
        renda_mensal=float(renda) if renda is not None else None,
        inicio_contrato=raw.get("inicio_contrato"),
        fim_contrato=raw.get("fim_contrato"),
        status=raw.get("status"),
        notas=raw.get("notas"),
        notion_id=raw.get("notion_id"),
        sem_fatura=sem_fatura,
    )


def _load_from_seed() -> list[Inquilino]:
    """Carrega inquilinos do JSON de fallback. Levanta se o ficheiro estiver corrompido."""
    if not SEED_PATH.exists():
        log.error("seed file nao existe em %s", SEED_PATH)
        return []
    try:
        with SEED_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.exception("falha a ler seed file %s: %s", SEED_PATH, exc)
        return []

    raw_list = payload.get("inquilinos") or []
    inquilinos: list[Inquilino] = []
    for raw in raw_list:
        item = _coerce_inquilino(raw)
        if item is not None:
            inquilinos.append(item)
    log.info("seed file carregado, %d inquilinos", len(inquilinos))
    return inquilinos


def _build_notion_client() -> NotionClient:
    # NotionClient() sem args usa NOTION_TOKEN do env (ver services/notion.py:13-26).
    return NotionClient()


async def load_inquilinos_from_notion(force_refresh: bool = False) -> list[Inquilino]:
    """
    Devolve a lista de inquilinos activos do predio Sopato.

    Comportamento:
        1. Se cache valida (TTL 1h) e nao force_refresh, devolve cache.
        2. Caso contrario, faz query a Notion (filtrada por Status != Inactivo).
        3. Se Notion falhar, faz fallback para seed JSON. Se a cache anterior
           existir, devolve a cache antiga em vez do seed (e mais fresca).
    """
    async with _cache_lock:
        now = time.time()
        cached_inquilinos: list[Inquilino] = _cache.get("inquilinos") or []
        cache_age = now - float(_cache.get("loaded_at") or 0.0)

        if not force_refresh and cached_inquilinos and cache_age < _CACHE_TTL_SECONDS:
            log.debug("cache hit, %d inquilinos, idade %.0fs", len(cached_inquilinos), cache_age)
            return list(cached_inquilinos)

        # NotionClient e async context manager (services/notion.py:34-40).
        try:
            async with _build_notion_client() as client:
                raw_list = await fetch_inquilinos_raw(client, only_active=True)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "falha a carregar inquilinos do Notion (%s); usando fallback", exc
            )
            if cached_inquilinos:
                log.info("a manter cache antiga (%d itens) por falha Notion", len(cached_inquilinos))
                return list(cached_inquilinos)
            seeded = _load_from_seed()
            # Nao actualiza loaded_at para forcar nova tentativa breve.
            _cache["inquilinos"] = seeded
            return list(seeded)

        inquilinos: list[Inquilino] = []
        for raw in raw_list:
            status = raw.get("status")
            if status and status not in STATUS_ACTIVOS:
                log.debug("inquilino %s skipped, status=%s", raw.get("nome"), status)
                continue
            item = _coerce_inquilino(raw)
            if item is not None:
                inquilinos.append(item)

        _cache["inquilinos"] = inquilinos
        _cache["loaded_at"] = now
        log.info(
            "inquilinos carregados de Notion: %d activos (force_refresh=%s)",
            len(inquilinos),
            force_refresh,
        )
        return list(inquilinos)


def get_cached_inquilinos() -> list[Inquilino]:
    """Acesso sincrono a cache, sem trigger de refresh. Lista vazia se nunca carregou."""
    return list(_cache.get("inquilinos") or [])


def listar_inquilinos_activos() -> list[Inquilino]:
    """
    API sincrona para workers que esperam a constante/funcao legacy
    (versao actual de verificacao_recibos_sopato no VPS chama isto sem await).

    Devolve a cache em memoria. Os workers que precisem de dados frescos
    devem fazer `await load_inquilinos_from_notion()` antes desta chamada.
    """
    return get_cached_inquilinos()


def is_sem_fatura(nome: str) -> bool:
    """Atalho para verificar se um inquilino nao emite fatura."""
    return nome in INQUILINOS_SEM_FATURA


def __getattr__(name: str) -> Any:
    """
    Backward compat: codigo antigo importava `from services.sopato_config import INQUILINOS`.
    Servimos a cache actual. Se nada carregado, devolve [].
    """
    if name == "INQUILINOS":
        return get_cached_inquilinos()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
