# telegram_api.py
import httpx
from typing import Any, Dict, Optional, List

API_BASE = "https://api.telegram.org"

async def tg_call(token: str, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_BASE}/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=params or {})
        resp.raise_for_status()
        return resp.json()

async def get_me(token: str):
    return await tg_call(token, "getMe")

async def get_updates(token: str, offset: Optional[int] = None, limit: Optional[int] = None):
    params = {}
    if offset is not None: params["offset"] = offset
    if limit is not None: params["limit"] = limit
    return await tg_call(token, "getUpdates", params)

async def set_webhook(token: str, url: str):
    return await tg_call(token, "setWebhook", {"url": url})

async def delete_webhook(token: str):
    return await tg_call(token, "deleteWebhook")

async def get_webhook_info(token: str):
    return await tg_call(token, "getWebhookInfo")

async def get_chat(token: str, chat_id: int):
    return await tg_call(token, "getChat", {"chat_id": chat_id})

async def get_chat_member(token: str, chat_id: int, user_id: int):
    return await tg_call(token, "getChatMember", {"chat_id": chat_id, "user_id": user_id})

async def get_chat_administrators(token: str, chat_id: int):
    return await tg_call(token, "getChatAdministrators", {"chat_id": chat_id})

async def get_chat_member_count(token: str, chat_id: int):
    return await tg_call(token, "getChatMemberCount", {"chat_id": chat_id})

async def ban_chat_member(token: str, chat_id: int, user_id: int, until_date: Optional[int] = None):
    params = {"chat_id": chat_id, "user_id": user_id}
    if until_date: params["until_date"] = until_date
    return await tg_call(token, "banChatMember", params)

async def unban_chat_member(token: str, chat_id: int, user_id: int):
    return await tg_call(token, "unbanChatMember", {"chat_id": chat_id, "user_id": user_id})

async def restrict_chat_member(token: str, chat_id: int, user_id: int, permissions: Dict[str, Any], until_date: Optional[int] = None):
    params = {"chat_id": chat_id, "user_id": user_id, "permissions": permissions}
    if until_date: params["until_date"] = until_date
    return await tg_call(token, "restrictChatMember", params)

async def leave_chat(token: str, chat_id: int):
    return await tg_call(token, "leaveChat", {"chat_id": chat_id})
