# crud.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from typing import Optional, List
from models import Bot, Chat, User, Membership, ActionLog

async def create_or_update_bot(session: AsyncSession, telegram_id: int, username: str, token: str) -> Bot:
    q = await session.execute(select(Bot).where(Bot.telegram_id == telegram_id))
    bot = q.scalars().first()
    if bot:
        bot.username = username or bot.username
        bot.token = token
    else:
        bot = Bot(telegram_id=telegram_id, username=username, token=token)
        session.add(bot)
    await session.commit()
    await session.refresh(bot)
    return bot

async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    q = await session.execute(select(Bot).where(Bot.id == bot_id))
    return q.scalars().first()

async def get_chat_by_telegram_id(session: AsyncSession, bot: Bot, telegram_chat_id: int) -> Optional[Chat]:
    q = await session.execute(select(Chat).where(
        Chat.bot_id == bot.id,
        Chat.telegram_chat_id == telegram_chat_id
    ))
    return q.scalars().first()

async def upsert_chat(session: AsyncSession, bot: Bot, chat_obj: dict) -> Chat:
    tg_chat_id = chat_obj["id"]
    q = await session.execute(select(Chat).where(Chat.bot_id == bot.id, Chat.telegram_chat_id == tg_chat_id))
    chat = q.scalars().first()
    if not chat:
        chat = Chat(
            bot_id=bot.id,
            telegram_chat_id=tg_chat_id,
            title=chat_obj.get("title") or chat_obj.get("username") or f"Chat {tg_chat_id}",
            type=chat_obj.get("type"),
            username=chat_obj.get("username"),
            last_seen=datetime.now(timezone.utc),
        )
        session.add(chat)
    else:
        chat.title = chat_obj.get("title") or chat.title
        chat.type = chat_obj.get("type") or chat.type
        chat.username = chat_obj.get("username") or chat.username
        chat.last_seen = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(chat)
    return chat

async def upsert_user(session: AsyncSession, user_obj: dict) -> User:
    tg_user_id = user_obj["id"]
    q = await session.execute(select(User).where(User.telegram_user_id == tg_user_id))
    user = q.scalars().first()
    if not user:
        user = User(
            telegram_user_id=tg_user_id,
            first_name=user_obj.get("first_name"),
            last_name=user_obj.get("last_name"),
            username=user_obj.get("username"),
            is_bot=user_obj.get("is_bot", False),
        )
        session.add(user)
    else:
        user.first_name = user_obj.get("first_name") or user.first_name
        user.last_name = user_obj.get("last_name") or user.last_name
        user.username = user_obj.get("username") or user.username
        user.is_bot = user_obj.get("is_bot", user.is_bot)
    await session.commit()
    await session.refresh(user)
    return user

async def upsert_membership(session: AsyncSession, bot: Bot, chat: Chat, user: User, status: str = "member", role: str = "member"):
    q = await session.execute(select(Membership).where(
        Membership.bot_id == bot.id,
        Membership.chat_id == chat.id,
        Membership.user_id == user.id
    ))
    membership = q.scalars().first()
    now = datetime.now(timezone.utc)
    if not membership:
        membership = Membership(
            bot_id=bot.id, chat_id=chat.id, user_id=user.id,
            status=status, role=role, joined_at=now, last_seen=now
        )
        session.add(membership)
    else:
        membership.role = role or membership.role
        membership.status = status or membership.status
        membership.last_seen = now
        if status == "left":
            membership.left_at = now
    await session.commit()
    await session.refresh(membership)
    return membership

async def log_action(session: AsyncSession, bot: Bot, chat: Optional[Chat], user_telegram_id: Optional[int], action: str, reason: Optional[str]=None, payload: Optional[str]=None):
    entry = ActionLog(
        bot_id=bot.id,
        chat_id=(chat.id if chat else None),
        user_telegram_id=user_telegram_id,
        action=action,
        reason=reason,
        payload=payload
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry

async def list_chats_for_bot(session: AsyncSession, bot: Bot):
    q = await session.execute(select(Chat).where(Chat.bot_id == bot.id))
    return q.scalars().all()

async def list_members_in_chat(session: AsyncSession, bot: Bot, chat_telegram_id: int):
    q = await session.execute(
        select(Membership)
        .join(Chat)
        .join(User)
        .where(
            Membership.bot_id == bot.id,
            Chat.telegram_chat_id == chat_telegram_id
        )
    )
    return q.scalars().all()

async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> Optional[User]:
    q = await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    return q.scalars().first()
