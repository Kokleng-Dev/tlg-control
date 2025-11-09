# main.py
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request, Path, Body
from pydantic import BaseModel
from core.db import Base, engine, get_session, AsyncSession
from models import Bot, Chat, User, ChatMember
from crud import (
    create_or_update_bot, list_admins_in_chat, list_bots_in_chat, list_humans_in_chat, upsert_chat, upsert_user, upsert_chat_member,
    log_action, list_chats_for_bot, list_chat_members_in_chat, get_bot_by_id,
    get_chat_by_telegram_id, get_user_by_telegram_id
)
from telegram_api import (
    get_me, get_updates, set_webhook, delete_webhook, get_webhook_info,
    ban_chat_member, unban_chat_member, restrict_chat_member,
    get_chat_member, get_chat_administrators, get_chat, get_chat_member_count
)
import asyncio
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

app = FastAPI(title="Telegram Control API")

# create DB tables on startup
@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ---------- Request models ----------
class RegisterBotIn(BaseModel):
    token: str

class SetWebhookIn(BaseModel):
    webhook_url: str

class ModifyUserIn(BaseModel):
    chat_id: int     # telegram chat id
    user_id: int     # telegram user id
    until_seconds: Optional[int] = None
    reason: Optional[str] = None

# ---------- Helper: Discover chats from getUpdates ----------
async def discover_chats_from_updates(bot: Bot, session: AsyncSession):
    """
    Fetch recent updates to discover chats the bot is in.
    This is best-effort and only gets recent activity.
    """
    discovered_chat_ids = set()
    try:
        updates_resp = await get_updates(bot.token, limit=100)
        if not updates_resp.get("ok"):
            return discovered_chat_ids

        for u in updates_resp.get("result", []):
            # Check multiple update types
            for field in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
                if field in u:
                    obj = u[field]
                    chat_obj = obj.get("chat")
                    if chat_obj:
                        chat = await upsert_chat(session, bot, chat_obj)
                        discovered_chat_ids.add(chat.telegram_chat_id)

                        # Handle new members
                        if obj.get("new_chat_members"):
                            for new_u in obj["new_chat_members"]:
                                user = await upsert_user(session, new_u)
                                await upsert_chat_member(session, bot, chat, user, status="member", role="member")

                        # Handle left members
                        if obj.get("left_chat_member"):
                            left = obj["left_chat_member"]
                            user = await upsert_user(session, left)
                            await upsert_chat_member(session, bot, chat, user, status="left", role="left")
                    break
    except Exception as e:
        print(f"Error discovering chats: {e}")

    return discovered_chat_ids

# ---------- Helper: Sync all members in a chat ----------
async def sync_chat_members(bot: Bot, chat: Chat, session: AsyncSession):
    """
    Fetch all administrators from a chat and sync to database.
    Note: Telegram API doesn't provide a way to get ALL members for large groups,
    only administrators. For full member list, you need to track via updates.
    """
    try:
        # Get all administrators
        admins_resp = await get_chat_administrators(bot.token, chat.telegram_chat_id)
        if not admins_resp.get("ok"):
            return {"admins": 0, "error": admins_resp.get("description")}

        admin_count = 0
        for admin_obj in admins_resp.get("result", []):
            user_obj = admin_obj.get("user")
            if not user_obj:
                continue

            user = await upsert_user(session, user_obj)
            status = admin_obj.get("status", "member")

            # Map Telegram status to our role
            role_map = {
                "creator": "creator",
                "administrator": "administrator",
                "member": "member",
                "restricted": "restricted",
                "left": "left",
                "kicked": "kicked"
            }
            role = role_map.get(status, "member")
            member_status = "member" if status in ("creator", "administrator", "member") else status

            await upsert_chat_member(session, bot, chat, user, status=member_status, role=role)
            admin_count += 1

        # Get member count
        try:
            count_resp = await get_chat_member_count(bot.token, chat.telegram_chat_id)
            member_count = count_resp.get("result", 0) if count_resp.get("ok") else 0
        except:
            member_count = admin_count

        return {"admins": admin_count, "total_members": member_count}

    except Exception as e:
        return {"error": str(e)}

# ---------- Bot registration ----------
@app.post("/bots/register")
async def register_bot(payload: RegisterBotIn, session: AsyncSession = Depends(get_session)):
    """
    Register a bot and discover all chats it's in.
    Note: Due to Telegram API limitations, we can only discover chats from recent updates.
    For complete chat list, the bot needs to be added to groups while connected.
    """
    token = payload.token.strip()

    # Validate token via getMe
    try:
        resp = await get_me(token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid token or telegram unreachable: {e}")

    if not resp.get("ok"):
        raise HTTPException(status_code=400, detail="Telegram returned not ok for getMe")

    me = resp["result"]

    # Store bot
    bot = await create_or_update_bot(
        session,
        telegram_id=me["id"],
        username=me.get("username"),
        token=token
    )

    # Discover chats from recent updates
    discovered_chat_ids = await discover_chats_from_updates(bot, session)

    # Sync members for each discovered chat
    chats = await list_chats_for_bot(session, bot)
    sync_results = {}
    for chat in chats:
        result = await sync_chat_members(bot, chat, session)
        sync_results[chat.telegram_chat_id] = result

    return {
        "message": "bot registered successfully",
        "bot_id": bot.id,
        "telegram_id": bot.telegram_id,
        "username": bot.username,
        "discovered_chats": len(discovered_chat_ids),
        "total_chats": len(chats),
        "sync_results": sync_results,
        "note": "Only recent chats are discovered. Add bot to groups and use sync-all-chats endpoint for complete list."
    }

# ---------- Set webhook (connect bot) ----------
@app.post("/bots/{bot_id}/connect")
async def connect_bot(
    bot_id: int,
    payload: SetWebhookIn,
    session: AsyncSession = Depends(get_session)
):
    """Connect bot by setting webhook"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    try:
        resp = await set_webhook(bot.token, payload.webhook_url)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("description", "Failed to set webhook"))

        await log_action(session, bot, None, None, "webhook_connected", payload=payload.webhook_url)
        return {
            "message": "Webhook set successfully",
            "webhook_url": payload.webhook_url,
            "response": resp
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to set webhook: {e}")

# ---------- Delete webhook (disconnect bot) ----------
@app.post("/bots/{bot_id}/disconnect")
async def disconnect_bot(bot_id: int, session: AsyncSession = Depends(get_session)):
    """Disconnect bot by deleting webhook"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    try:
        resp = await delete_webhook(bot.token)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("description", "Failed to delete webhook"))

        await log_action(session, bot, None, None, "webhook_disconnected")
        return {
            "message": "Webhook deleted successfully",
            "response": resp
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to delete webhook: {e}")

# ---------- Get webhook status ----------
@app.get("/bots/{bot_id}/webhook-status")
async def webhook_status(bot_id: int, session: AsyncSession = Depends(get_session)):
    """Get current webhook status"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    try:
        resp = await get_webhook_info(bot.token)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail="Failed to get webhook info")

        info = resp.get("result", {})
        return {
            "connected": bool(info.get("url")),
            "webhook_url": info.get("url"),
            "has_custom_certificate": info.get("has_custom_certificate"),
            "pending_update_count": info.get("pending_update_count"),
            "last_error_date": info.get("last_error_date"),
            "last_error_message": info.get("last_error_message"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get webhook status: {e}")

# ---------- Sync all chats and members ----------
@app.post("/bots/{bot_id}/sync-all-chats")
async def sync_all_chats(bot_id: int, session: AsyncSession = Depends(get_session)):
    """
    Discover and sync all chats for this bot.
    This will fetch recent updates and sync members for all known chats.
    """
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Discover new chats from updates
    discovered_chat_ids = await discover_chats_from_updates(bot, session)

    # Sync all chats
    chats = await list_chats_for_bot(session, bot)
    sync_results = {}
    for chat in chats:
        result = await sync_chat_members(bot, chat, session)
        sync_results[str(chat.telegram_chat_id)] = {
            "title": chat.title,
            **result
        }

    return {
        "message": "Sync completed",
        "newly_discovered_chats": len(discovered_chat_ids),
        "total_chats": len(chats),
        "sync_results": sync_results
    }

# ---------- Sync specific chat members ----------
@app.post("/bots/{bot_id}/chats/{chat_telegram_id}/sync-members")
async def sync_chat_members_endpoint(
    bot_id: int,
    chat_telegram_id: int,
    session: AsyncSession = Depends(get_session)
):
    """Sync all members (administrators) for a specific chat"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    chat = await get_chat_by_telegram_id(session, bot, chat_telegram_id)
    if not chat:
        # Try to fetch chat info from Telegram
        try:
            chat_resp = await get_chat(bot.token, chat_telegram_id)
            if chat_resp.get("ok"):
                chat = await upsert_chat(session, bot, chat_resp["result"])
            else:
                raise HTTPException(status_code=404, detail="Chat not found")
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Chat not found: {e}")

    result = await sync_chat_members(bot, chat, session)
    return {
        "chat_id": chat.telegram_chat_id,
        "title": chat.title,
        **result
    }

# ---------- Webhook handler ----------
@app.post("/webhook/{bot_id}")
async def webhook_handler(
    bot_id: int = Path(..., description="internal bot id"),
    request: Request = None,
    session: AsyncSession = Depends(get_session)
):
    body = await request.json()
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not registered in system")

    # MESSAGE: new_chat_members / left_chat_member
    if "message" in body:
        message = body["message"]
        chat_obj = message.get("chat")
        ch = None
        if chat_obj:
            ch = await upsert_chat(session, bot, chat_obj)

        if message.get("new_chat_members"):
            for new_u in message["new_chat_members"]:
                user = await upsert_user(session, new_u)
                if ch:
                    await upsert_chat_member(session, bot, ch, user, status="member", role="member")
                    await log_action(session, bot, ch, new_u.get("id"), "join", payload=str(message))

        if message.get("left_chat_member"):
            left = message["left_chat_member"]
            user = await upsert_user(session, left)
            if ch:
                await upsert_chat_member(session, bot, ch, user, status="left", role="left")
                await log_action(session, bot, ch, left.get("id"), "left", payload=str(message))

    # CHAT_MEMBER: detects member status changes
    if "chat_member" in body:
        cm = body["chat_member"]
        chat_obj = cm.get("chat")
        new_chat_member = cm.get("new_chat_member") or {}
        user_obj = new_chat_member.get("user") or cm.get("from")
        status = new_chat_member.get("status")
        ch = None
        if chat_obj:
            ch = await upsert_chat(session, bot, chat_obj)
        if user_obj:
            user = await upsert_user(session, user_obj)
            if status in ("member", "administrator", "creator"):
                role = status
                await upsert_chat_member(session, bot, ch, user, status="member", role=role)
                await log_action(session, bot, ch, user.telegram_user_id, "chat_member_update", payload=str(cm))
            elif status in ("left", "kicked"):
                await upsert_chat_member(session, bot, ch, user, status="left", role="left")
                await log_action(session, bot, ch, user.telegram_user_id, "chat_member_update_left", payload=str(cm))

    # MY_CHAT_MEMBER: changes to the bot's status in the chat
    if "my_chat_member" in body:
        mc = body["my_chat_member"]
        chat_obj = mc.get("chat")
        ch = None
        if chat_obj:
            ch = await upsert_chat(session, bot, chat_obj)
        await log_action(session, bot, ch, None, "my_chat_member", payload=str(mc))

    return {"ok": True}

# ---------- List chats for bot ----------
@app.get("/bots/{bot_id}/chats")
async def list_chats(bot_id: int, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    chats = await list_chats_for_bot(session, bot)
    return {
        "chats": [
            {
                "id": c.id,
                "telegram_chat_id": c.telegram_chat_id,
                "title": c.title,
                "type": c.type,
                "username": c.username,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None
            } for c in chats
        ]
    }


# ---------- List members in a chat (from DB) ----------
@app.get("/bots/{bot_id}/chats/{chat_telegram_id}/members")
async def list_members(
    bot_id: int,
    chat_telegram_id: int,
    filter_type: Optional[str] = None,  # ✅ NEW: "bots", "humans", "admins"
    session: AsyncSession = Depends(get_session)
):
    """
    List all members in a chat with optional filtering.

    Query params:
    - filter_type: "bots" | "humans" | "admins" | null (all)
    """
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")

    # ✅ Use optimized functions based on filter
    if filter_type == "bots":
        members = await list_bots_in_chat(session, bot, chat_telegram_id)
    elif filter_type == "humans":
        members = await list_humans_in_chat(session, bot, chat_telegram_id)
    elif filter_type == "admins":
        members = await list_admins_in_chat(session, bot, chat_telegram_id)
    else:
        members = await list_chat_members_in_chat(session, bot, chat_telegram_id)

    out = []
    for m in members:
        badges = []
        badge_color = "gray"

        # Use is_bot from chat_members table directly! ✅
        is_bot = m.is_bot
        is_current_bot = (m.user.telegram_user_id == bot.telegram_id)

        # Role badges
        if m.role == "creator":
            badges.append("OWNER")
            badge_color = "red"
        elif m.role == "administrator":
            badges.append("ADMIN")
            badge_color = "blue"

        # Bot indicator
        if is_bot:
            badges.append("BOT")
            if not badges or badges == ["BOT"]:
                badge_color = "green"
            else:
                badge_color = "purple"

        if is_current_bot:
            badges.append("THIS BOT")

        # Status badges
        if m.is_muted:
            badges.append("MUTED")
        if m.status == "banned":
            badges.append("BANNED")
            badge_color = "black"
        elif m.status == "restricted":
            badges.append("RESTRICTED")
            badge_color = "orange"
        elif m.status == "left":
            badges.append("LEFT")
            badge_color = "gray"

        out.append({
            "user_telegram_id": m.user.telegram_user_id,
            "username": m.user.username,
            "first_name": m.user.first_name,
            "last_name": m.user.last_name,
            "full_name": f"{m.user.first_name or ''} {m.user.last_name or ''}".strip(),
            "is_bot": is_bot,  # ✅ From chat_members table directly
            "is_current_bot": is_current_bot,
            "role": m.role,
            "status": m.status,
            "is_muted": m.is_muted,
            "badges": badges,
            "badge_color": badge_color,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "left_at": m.left_at.isoformat() if m.left_at else None,
            "last_seen": m.last_seen.isoformat() if m.last_seen else None,
        })

    # Sort
    def sort_key(member):
        if member["is_current_bot"]:
            return (0, member["full_name"])
        role_priority = {"creator": 1, "administrator": 2, "member": 3}
        return (role_priority.get(member["role"], 99), member["full_name"])

    out.sort(key=sort_key)

    return {"members": out, "total": len(out)}

# ✅ NEW: Get statistics about chat members
@app.get("/bots/{bot_id}/chats/{chat_telegram_id}/stats")
async def get_chat_stats(
    bot_id: int,
    chat_telegram_id: int,
    session: AsyncSession = Depends(get_session)
):
    """Get statistics about chat members"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")

    # Fast queries using is_bot index
    all_members = await list_chat_members_in_chat(session, bot, chat_telegram_id)
    bots = await list_bots_in_chat(session, bot, chat_telegram_id)
    humans = await list_humans_in_chat(session, bot, chat_telegram_id)
    admins = await list_admins_in_chat(session, bot, chat_telegram_id)

    # Count by status
    active = [m for m in all_members if m.status == "member"]
    left = [m for m in all_members if m.status == "left"]
    banned = [m for m in all_members if m.status == "banned"]
    muted = [m for m in all_members if m.is_muted]

    return {
        "total_members": len(all_members),
        "total_bots": len(bots),
        "total_humans": len(humans),
        "total_admins": len(admins),
        "active_members": len(active),
        "left_members": len(left),
        "banned_members": len(banned),
        "muted_members": len(muted),
    }

# ---------- Ban ----------
@app.post("/bots/{bot_id}/ban")
async def ban_user(bot_id: int, body: ModifyUserIn, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    until_date = None
    if body.until_seconds:
        until_date = int((datetime.now(timezone.utc) + timedelta(seconds=body.until_seconds)).timestamp())
    try:
        resp = await ban_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id, until_date=until_date)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")

    # Log and update membership
    chat = await get_chat_by_telegram_id(session, bot, body.chat_id)
    await log_action(session, bot, chat, body.user_id, "ban", reason=body.reason, payload=str(resp))

    user = await get_user_by_telegram_id(session, body.user_id)
    if user and chat:
        await upsert_chat_member(session, bot, chat, user, status="banned", role="kicked")

    return {"ok": True, "response": resp}

# ---------- Unban ----------
@app.post("/bots/{bot_id}/unban")
async def unban_user(bot_id: int, body: ModifyUserIn, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    try:
        resp = await unban_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")

    chat = await get_chat_by_telegram_id(session, bot, body.chat_id)
    await log_action(session, bot, chat, body.user_id, "unban", payload=str(resp))

    user = await get_user_by_telegram_id(session, body.user_id)
    if user and chat:
        await upsert_chat_member(session, bot, chat, user, status="left", role="left")

    return {"ok": True, "response": resp}

# ---------- Mute (restrict) ----------
@app.post("/bots/{bot_id}/mute")
async def mute_user(bot_id: int, body: ModifyUserIn, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    permissions = {
        "can_send_messages": False,
        "can_send_media_messages": False,
        "can_send_polls": False,
        "can_send_other_messages": False,
        "can_send_other_messages": False,
        "can_add_web_page_previews": False
    }
    until_date = None
    if body.until_seconds:
        until_date = int((datetime.now(timezone.utc) + timedelta(seconds=body.until_seconds)).timestamp())
    try:
        resp = await restrict_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id, permissions=permissions, until_date=until_date)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")

    chat = await get_chat_by_telegram_id(session, bot, body.chat_id)
    await log_action(session, bot, chat, body.user_id, "mute", reason=body.reason, payload=str(resp))

    user = await get_user_by_telegram_id(session, body.user_id)
    if user and chat:
        membership = await upsert_chat_member(session, bot, chat, user, status="restricted", role="restricted")
        membership.is_muted = True
        await session.commit()

    return {"ok": True, "response": resp}

# ---------- Unmute ----------
@app.post("/bots/{bot_id}/unmute")
async def unmute_user(bot_id: int, body: ModifyUserIn, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    permissions = {
        "can_send_messages": True,
        "can_send_media_messages": True,
        "can_send_polls": True,
        "can_send_other_messages": True,
        "can_add_web_page_previews": True
    }
    try:
        resp = await restrict_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id, permissions=permissions)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")

    chat = await get_chat_by_telegram_id(session, bot, body.chat_id)
    await log_action(session, bot, chat, body.user_id, "unmute", payload=str(resp))

    user = await get_user_by_telegram_id(session, body.user_id)
    if user and chat:
        membership = await upsert_chat_member(session, bot, chat, user, status="member", role="member")
        membership.is_muted = False
        await session.commit()

    return {"ok": True, "response": resp}

# ---------- Kick (ban then unban immediately) ----------
@app.post("/bots/{bot_id}/kick")
async def kick_user(bot_id: int, body: ModifyUserIn, session: AsyncSession = Depends(get_session)):
    """Kick user from group (ban then unban so they can rejoin)"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")

    try:
        # First ban
        ban_resp = await ban_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id)
        # Then unban immediately
        unban_resp = await unban_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")

    chat = await get_chat_by_telegram_id(session, bot, body.chat_id)
    await log_action(session, bot, chat, body.user_id, "kick", reason=body.reason, payload=str(unban_resp))

    user = await get_user_by_telegram_id(session, body.user_id)
    if user and chat:
        await upsert_chat_member(session, bot, chat, user, status="left", role="left")

    return {"ok": True, "response": unban_resp}

# ---------- Get bot info ----------
@app.get("/bots/{bot_id}")
async def get_bot_info(bot_id: int, session: AsyncSession = Depends(get_session)):
    """Get bot information"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Count chats and members
    chats = await list_chats_for_bot(session, bot)
    total_members = 0
    for chat in chats:
        members = await list_chat_members_in_chat(session, bot, chat.telegram_chat_id)
        total_members += len(members)

    return {
        "id": bot.id,
        "telegram_id": bot.telegram_id,
        "username": bot.username,
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
        "total_chats": len(chats),
        "total_members": total_members
    }

# ---------- List all bots ----------
@app.get("/bots")
async def list_bots(session: AsyncSession = Depends(get_session)):
    """List all registered bots"""
    q = await session.execute(select(Bot))
    bots = q.scalars().all()
    return {
        "bots": [
            {
                "id": b.id,
                "telegram_id": b.telegram_id,
                "username": b.username,
                "created_at": b.created_at.isoformat() if b.created_at else None
            } for b in bots
        ]
    }

# ---------- Delete bot ----------
@app.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, session: AsyncSession = Depends(get_session)):
    """Delete a bot and all associated data"""
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    await session.delete(bot)
    await session.commit()

    return {"message": "Bot deleted successfully", "bot_id": bot_id}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
