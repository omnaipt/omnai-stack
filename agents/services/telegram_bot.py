"""Telegram bot para push de alertas P0.

Comandos do user:
  /start   - regista chat_id para receber alertas
  /inbox   - mostra contadores actuais
  /pause   - pausa alertas temporariamente
  /resume  - retoma alertas
  /help    - ajuda

Botoes inline em cada P0:
  ✓ Resolver  -> marca done na DB
  ⏰ Snooze 7d -> empurra 7 dias
  🗑 Dispensar -> marca dismissed
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from services.briefing_db import _get_pool

log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
BASE_URL = os.getenv("ACTIONS_BASE_URL", "https://agents.omnai.pt")
NOTION_BRIEFING_URL = "https://www.notion.so/33e973b9238781078667eadc9128ab27"

API = f"https://api.telegram.org/bot{TOKEN}"


def _enabled() -> bool:
    return bool(TOKEN)


URGENCIA_EMOJI = {"P0": "🔴", "P1": "🟡", "P2": "🟢", "P3": "⚪"}
URGENCIA_LABEL = {"P0": "URGENTE", "P1": "ESTA SEMANA", "P2": "ACOMPANHAR", "P3": "OUTROS"}


# ---------- baixo nivel: chamadas API Telegram ----------

async def _post(method: str, payload: dict) -> dict:
    if not _enabled():
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN nao definido"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{API}/{method}", json=payload)
            data = r.json()
            if not data.get("ok"):
                log.warning("telegram.%s_failed %s", method, data)
            return data
    except Exception as exc:
        log.warning("telegram.%s_exception err=%s", method, exc)
        return {"ok": False, "error": str(exc)}


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _post("sendMessage", payload)


async def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await _post("editMessageText", payload)


async def answer_callback(callback_id: str, text: str = "", show_alert: bool = False) -> dict:
    return await _post("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
        "show_alert": show_alert,
    })


async def set_webhook(url: str) -> dict:
    payload = {
        "url": url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    return await _post("setWebhook", payload)


async def delete_webhook() -> dict:
    return await _post("deleteWebhook", {"drop_pending_updates": True})


async def get_webhook_info() -> dict:
    return await _post("getWebhookInfo", {})


# ---------- registo de chat_id ----------

async def register_user(chat_id: int, username: str | None = None, first_name: str | None = None) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO telegram_users (chat_id, username, first_name, last_seen_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, telegram_users.username),
                first_name = COALESCE(EXCLUDED.first_name, telegram_users.first_name),
                last_seen_at = NOW(),
                enabled = TRUE
            """,
            chat_id, username, first_name,
        )
    return True


async def set_user_enabled(chat_id: int, enabled: bool) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE telegram_users SET enabled = $2 WHERE chat_id = $1",
            chat_id, enabled,
        )
    return res.endswith(" 1")


async def list_active_users(urgencia: str = "P0") -> list[int]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT chat_id FROM telegram_users WHERE enabled = TRUE AND $1 = ANY(urgencias)",
            urgencia,
        )
        return [r["chat_id"] for r in rows]


# ---------- formato das mensagens ----------

def _format_item(item: dict) -> str:
    urg = item.get("urgencia", "P2")
    emoji = URGENCIA_EMOJI.get(urg, "⚪")
    label = URGENCIA_LABEL.get(urg, "")
    titulo = item.get("titulo") or "(sem titulo)"
    detalhe = item.get("detalhe") or ""
    empresa = item.get("empresa") or ""

    parts = [f"<b>{emoji} {label}</b>"]
    parts.append(f"<b>{_escape(titulo)}</b>")
    if empresa:
        parts.append(f"<i>{_escape(empresa)}</i>")
    if detalhe:
        parts.append("")
        parts.append(_escape(detalhe[:400]))
    return "\n".join(parts)


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_keyboard(item_id: str, with_open: bool = True) -> dict:
    rows = [
        [
            {"text": "✓ Resolver", "callback_data": f"done:{item_id}"},
            {"text": "⏰ Snooze 7d", "callback_data": f"snooze:{item_id}:7"},
        ],
        [
            {"text": "🗑 Dispensar", "callback_data": f"dismiss:{item_id}"},
        ],
    ]
    if with_open:
        rows.append([{"text": "🔗 Abrir inbox", "url": f"{BASE_URL}/inbox"}])
    return {"inline_keyboard": rows}


# ---------- envio de P0 ----------

async def send_p0_alert(item: dict) -> int:
    """Envia alerta para todos os chat_ids subscritos para a urgencia do item.
    Retorna numero de envios bem sucedidos.
    """
    urg = item.get("urgencia", "P2")
    chats = await list_active_users(urgencia=urg)
    if not chats:
        log.info("telegram.no_subscribers urg=%s", urg)
        return 0

    text = _format_item(item)
    item_id = str(item.get("id", ""))
    markup = _inline_keyboard(item_id)

    sent = 0
    for chat_id in chats:
        r = await send_message(chat_id, text, reply_markup=markup)
        if r.get("ok"):
            sent += 1
    log.info("telegram.p0_sent urg=%s sent=%d total=%d", urg, sent, len(chats))
    return sent


# ---------- handlers ----------

WELCOME_TEXT = (
    "🦉 <b>OMNAI Inbox Bot</b>\n\n"
    "Vais receber aqui notificações de itens críticos (P0) do teu inbox executivo.\n\n"
    "<b>Comandos:</b>\n"
    "/inbox - ver contadores actuais\n"
    "/pause - pausar alertas\n"
    "/resume - retomar alertas\n"
    "/help - ajuda\n\n"
    "Cada alerta tem botões inline (Resolver, Snooze, Dispensar) para agires sem abrir o dashboard."
)


async def handle_message(message: dict) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return
    text = (message.get("text") or "").strip()
    username = message.get("from", {}).get("username")
    first_name = chat.get("first_name")

    if text.startswith("/start"):
        await register_user(chat_id, username, first_name)
        await send_message(chat_id, WELCOME_TEXT)
    elif text.startswith("/inbox"):
        from services import briefing_db
        stats = await briefing_db.stats_por_urgencia()
        total = sum(stats.values())
        msg = (
            f"<b>📥 Inbox</b>\n"
            f"Total aberto: <b>{total}</b>\n\n"
            f"🔴 P0: {stats.get('P0', 0)}\n"
            f"🟡 P1: {stats.get('P1', 0)}\n"
            f"🟢 P2: {stats.get('P2', 0)}\n"
            f"⚪ P3: {stats.get('P3', 0)}\n\n"
            f"<a href=\"{BASE_URL}/inbox\">Abrir dashboard</a>"
        )
        await send_message(chat_id, msg)
    elif text.startswith("/pause"):
        await set_user_enabled(chat_id, False)
        await send_message(chat_id, "🔕 Alertas pausados. Usa /resume para retomar.")
    elif text.startswith("/resume"):
        await set_user_enabled(chat_id, True)
        await send_message(chat_id, "🔔 Alertas activos.")
    elif text.startswith("/help"):
        await send_message(chat_id, WELCOME_TEXT)
    else:
        await send_message(
            chat_id,
            "Não percebi o comando. Usa /help para ver as opções."
        )


async def handle_callback(callback_query: dict) -> None:
    cb_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    parts = data.split(":")
    action = parts[0] if parts else ""
    item_id = parts[1] if len(parts) > 1 else ""

    from services import briefing_db

    if action == "done":
        ok = await briefing_db.mark_done(item_id)
        feedback = "✓ Resolvido" if ok else "Já estava tratado"
    elif action == "dismiss":
        ok = await briefing_db.mark_dismissed(item_id)
        feedback = "🗑 Dispensado" if ok else "Sem efeito"
    elif action == "snooze":
        days = int(parts[2]) if len(parts) > 2 else 7
        ok = await briefing_db.snooze(item_id, days=days)
        feedback = f"⏰ Snoozed {days}d" if ok else "Sem efeito"
    else:
        feedback = "Acção desconhecida"
        ok = False

    await answer_callback(cb_id, text=feedback, show_alert=False)

    if ok and chat_id and message_id:
        # Editar a mensagem para mostrar o estado novo (sem botões)
        try:
            await edit_message_text(
                chat_id, message_id,
                text=(message.get("text", "") or "")[:500] + f"\n\n<i>{feedback}</i>",
                reply_markup={"inline_keyboard": [
                    [{"text": "Abrir inbox", "url": f"{BASE_URL}/inbox"}]
                ]},
            )
        except Exception:
            pass


async def process_update(update: dict) -> None:
    """Dispatch a partir de webhook payload."""
    if "callback_query" in update:
        await handle_callback(update["callback_query"])
    elif "message" in update:
        await handle_message(update["message"])
