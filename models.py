# models.py
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Text, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from datetime import datetime
from core.db import Base

class Bot(Base):
    __tablename__ = "bots"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    token = Column(Text, nullable=False)  # <-- encrypt in production
    created_at = Column(DateTime, default=datetime.utcnow)

    chats = relationship("Chat", back_populates="bot", cascade="all, delete-orphan")
    actions = relationship("ActionLog", back_populates="bot")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id", ondelete="CASCADE"), nullable=False)
    telegram_chat_id = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=True)
    type = Column(String, nullable=True)
    username = Column(String, nullable=True)
    last_seen = Column(DateTime, default=datetime.utcnow)

    bot = relationship("Bot", back_populates="chats")
    memberships = relationship("Membership", back_populates="chat", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("bot_id", "telegram_chat_id", name="uix_bot_chat"),)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(Integer, unique=True, index=True, nullable=False)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    is_bot = Column(Boolean, default=False)

    memberships = relationship("Membership", back_populates="user", cascade="all, delete-orphan")

class Membership(Base):
    __tablename__ = "memberships"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, nullable=False)
    chat_id = Column(Integer, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    status = Column(String, default="member")  # member, left, restricted, banned
    role = Column(String, default="member")  # creator, administrator, member, restricted, left, kicked
    is_muted = Column(Boolean, default=False)

    joined_at = Column(DateTime, default=datetime.utcnow)
    left_at = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="memberships")
    chat = relationship("Chat", back_populates="memberships")

    __table_args__ = (UniqueConstraint("bot_id", "chat_id", "user_id", name="uix_membership"),)

class ActionLog(Base):
    __tablename__ = "action_logs"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(Integer, ForeignKey("chats.id", ondelete="SET NULL"), nullable=True)
    user_telegram_id = Column(Integer, nullable=True)
    action = Column(String, nullable=False)  # ban, unban, mute, unmute, join, left, role_change...
    reason = Column(String, nullable=True)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    bot = relationship("Bot", back_populates="actions")
