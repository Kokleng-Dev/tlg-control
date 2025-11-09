# main.py
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request, Path, Body
from pydantic import BaseModel
from core.db import Base, engine, get_session, AsyncSession
from models import Bot, Chat, User, Membership
from crud import (
    create_or_update_bot, upsert_chat, upsert_user, upsert_membership,
    log_action, list_chats_for_bot, list_members_in_chat, get_bot_by_id
)
from telegram_api import (
    get_me, get_updates, set_webhook, ban_chat_member, unban_chat_member, restrict_chat_member, get_chat_member, get_chat_administrators
)
import asyncio
from typing import Optional, List
from datetime import datetime, timedelta
from sqlalchemy import select

app = FastAPI(title="Telegram Control API")

# create DB tables on startup (quick dev setup)
@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ---------- Request models ----------
class RegisterBotIn(BaseModel):
    token: str

class ModifyUserIn(BaseModel):
    chat_id: int     # telegram chat id
    user_id: int     # telegram user id
    until_seconds: Optional[int] = None
    reason: Optional[str] = None

# ---------- Bot registration ----------
@app.post("/bots/register")
async def register_bot(payload: RegisterBotIn, session: AsyncSession = Depends(get_session)):
    token = payload.token.strip()
    # validate token via getMe
    try:
        resp = await get_me(token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid token or telegram unreachable: {e}")

    if not resp.get("ok"):
        raise HTTPException(status_code=400, detail="Telegram returned not ok for getMe")
    me = resp["result"]
    # store bot
    bot = await create_or_update_bot(session, telegram_id=me["id"], username=me.get("username"), token=token)

    # try to discover chats via getUpdates (best-effort)
    try:
        updates_resp = await get_updates(token, limit=50)
    except Exception:
        updates_resp = None

    if updates_resp and updates_resp.get("ok"):
        for u in updates_resp.get("result", []):
            # check multiple possible update types
            obj = None
            for field in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
                if field in u:
                    obj = u[field]
                    break
            if not obj:
                continue
            chat = obj.get("chat")
            if chat:
                await upsert_chat(session, bot, chat)
            # handle nested new_chat_members / left_chat_member inside message
            if obj.get("new_chat_members"):
                for new_u in obj["new_chat_members"]:
                    user = await upsert_user(session, new_u)
                    ch = await upsert_chat(session, bot, obj["chat"])
                    await upsert_membership(session, bot, ch, user, status="member", role="member")
            if obj.get("left_chat_member"):
                left = obj["left_chat_member"]
                user = await upsert_user(session, left)
                ch = await upsert_chat(session, bot, obj["chat"])
                await upsert_membership(session, bot, ch, user, status="left", role="left")
    return {"message": "bot registered", "bot_id": bot.id, "username": bot.username}

# optional: set webhook from system (not required, user can set manually)
@app.post("/bots/{bot_id}/set-webhook")
async def api_set_webhook(bot_id: int, webhook_url: str = Body(...), session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    resp = await set_webhook(bot.token, webhook_url)
    return resp

# ---------- Webhook handler ----------
@app.post("/webhook/{bot_id}")
async def webhook_handler(bot_id: int = Path(..., description="internal bot id"), request: Request = None, session: AsyncSession = Depends(get_session)):
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
                    await upsert_membership(session, bot, ch, user, status="member", role="member")
                    await log_action(session, bot, ch, new_u.get("id"), "join", payload=str(message))
        if message.get("left_chat_member"):
            left = message["left_chat_member"]
            user = await upsert_user(session, left)
            if ch:
                await upsert_membership(session, bot, ch, user, status="left", role="left")
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
                await upsert_membership(session, bot, ch, user, status="member", role=role)
                await log_action(session, bot, ch, user.telegram_user_id if hasattr(user, "telegram_user_id") else user_obj.get("id"), "chat_member_update", payload=str(cm))
            elif status in ("left", "kicked"):
                await upsert_membership(session, bot, ch, user, status="left", role="left")
                await log_action(session, bot, ch, user.telegram_user_id if hasattr(user, "telegram_user_id") else user_obj.get("id"), "chat_member_update_left", payload=str(cm))

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
async def list_members(bot_id: int, chat_telegram_id: int, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    members = await list_members_in_chat(session, bot, chat_telegram_id)
    out = []
    for m in members:
        out.append({
            "user_telegram_id": m.user.telegram_user_id,
            "username": m.user.username,
            "first_name": m.user.first_name,
            "role": m.role,
            "status": m.status,
            "is_muted": m.is_muted,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "left_at": m.left_at.isoformat() if m.left_at else None,
        })
    return {"members": out}

# ---------- Sync roles by calling getChatAdministrators (useful to show badges) ----------
@app.post("/bots/{bot_id}/chats/{chat_telegram_id}/sync-admins")
async def sync_admins(bot_id: int, chat_telegram_id: int, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    try:
        resp = await get_chat_administrators(bot.token, chat_telegram_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram API error: {e}")
    if not resp.get("ok"):
        raise HTTPException(status_code=400, detail="Telegram returned not ok")
    admins = resp.get("result", [])
    # map admins by user id
    admin_ids = set()
    for a in admins:
        user = a.get("user")
        if not user:
            continue
        admin_ids.add(user["id"])
        # upsert user and membership role
        u = await upsert_user(session, user)
        # ensure chat object exists
        from crud import upsert_chat as upsert_chat_func
        q = await session.execute(select(Chat).where(Chat.bot_id == bot.id, Chat.telegram_chat_id == chat_telegram_id))
        chat = q.scalars().first()
        if not chat:
            # if chat unknown, create minimal chat record
            chat_obj = {"id": chat_telegram_id, "title": None, "type": None}
            chat = await upsert_chat(session, bot, chat_obj)
        await upsert_membership(session, bot, chat, u, status="member", role="administrator")
    # optionally set role="member" for other users in DB (we won't change all others here)
    return {"updated_admin_count": len(admin_ids)}

# ---------- Ban ----------
@app.post("/bots/{bot_id}/ban")
async def ban_user(bot_id: int, body: ModifyUserIn, session: AsyncSession = Depends(get_session)):
    bot = await get_bot_by_id(session, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot not found")
    until_date = None
    if body.until_seconds:
        until_date = int((datetime.utcnow() + timedelta(seconds=body.until_seconds)).timestamp())
    try:
        resp = await ban_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id, until_date=until_date)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")
    # log and update membership
    q = await session.execute(select(Chat).where(Chat.bot_id == bot.id, Chat.telegram_chat_id == body.chat_id))
    chat = q.scalars().first()
    await log_action(session, bot, chat, body.user_id, "ban", reason=body.reason, payload=str(resp))
    # mark membership as banned
    q2 = await session.execute(select(User).where(User.telegram_user_id == body.user_id))
    user = q2.scalars().first()
    if user and chat:
        await upsert_membership(session, bot, chat, user, status="banned", role="kicked")
    return {"ok": resp}

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
    q = await session.execute(select(Chat).where(Chat.bot_id == bot.id, Chat.telegram_chat_id == body.chat_id))
    chat = q.scalars().first()
    await log_action(session, bot, chat, body.user_id, "unban", payload=str(resp))
    # update membership to left or member (we set left)
    q2 = await session.execute(select(User).where(User.telegram_user_id == body.user_id))
    user = q2.scalars().first()
    if user and chat:
        await upsert_membership(session, bot, chat, user, status="left", role="left")
    return {"ok": resp}

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
        "can_add_web_page_previews": False
    }
    until_date = None
    if body.until_seconds:
        until_date = int((datetime.utcnow() + timedelta(seconds=body.until_seconds)).timestamp())
    try:
        resp = await restrict_chat_member(bot.token, chat_id=body.chat_id, user_id=body.user_id, permissions=permissions, until_date=until_date)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Telegram error: {e}")
    q = await session.execute(select(Chat).where(Chat.bot_id == bot.id, Chat.telegram_chat_id == body.chat_id))
    chat = q.scalars().first()
    await log_action(session, bot, chat, body.user_id, "mute", reason=body.reason, payload=str(resp))
    # update membership is_muted flag
    q2 = await session.execute(select(User).where(User.telegram_user_id == body.user_id))
    user = q2.scalars().first()
    if user and chat:
        membership = await upsert_membership(session, bot, chat, user, status="restricted", role="restricted")
        membership.is_muted = True
        await session.commit()
    return {"ok": resp}

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
    q = await session.execute(select(Chat).where(Chat.bot_id == bot.id, Chat.telegram_chat_id == body.chat_id))
    chat = q.scalars().first()
    await log_action(session, bot, chat, body.user_id, "unmute", payload=str(resp))
    q2 = await session.execute(select(User).where(User.telegram_user_id == body.user_id))
    user = q2.scalars().first()
    if user and chat:
        membership = await upsert_membership(session, bot, chat, user, status="member", role="member")
        membership.is_muted = False
        await session.commit()
    return {"ok": resp}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
