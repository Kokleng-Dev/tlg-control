# models.py
from sqlalchemy import Column, BigInteger, String, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from core.db import Base

class Bot(Base):
    __tablename__ = "bots"
    id = Column(BigInteger, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True)
    username = Column(String(255))
    token = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    chats = relationship("Chat", back_populates="bot", cascade="all, delete-orphan")
    chat_members = relationship("ChatMember", back_populates="bot")
    logs = relationship("ActionLog", back_populates="bot")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(BigInteger, primary_key=True, index=True)
    bot_id = Column(BigInteger, ForeignKey("bots.id", ondelete="CASCADE"))
    telegram_chat_id = Column(BigInteger, index=True)
    title = Column(String(255))
    type = Column(String(50))
    username = Column(String(255), nullable=True)
    last_seen = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    bot = relationship("Bot", back_populates="chats")
    chat_members = relationship("ChatMember", back_populates="chat")
    logs = relationship("ActionLog", back_populates="chat")

class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, unique=True, index=True)
    first_name = Column(String(255))
    last_name = Column(String(255))
    username = Column(String(255), nullable=True)
    is_bot = Column(Boolean, default=False)

    chat_members = relationship("ChatMember", back_populates="user")

class ChatMember(Base):
    """
    Represents a user's membership in a chat for a specific bot.
    Tracks their role, status, and activity timestamps.
    """
    __tablename__ = "chat_members"
    id = Column(BigInteger, primary_key=True, index=True)
    bot_id = Column(BigInteger, ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))

    role = Column(String(50), default="member")  # creator, administrator, member, restricted, left, kicked
    status = Column(String(50), default="member")  # member, left, banned, restricted
    is_muted = Column(Boolean, default=False)
    joined_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    left_at = Column(DateTime(timezone=True), nullable=True)
    last_seen = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    bot = relationship("Bot", back_populates="chat_members")
    chat = relationship("Chat", back_populates="chat_members")
    user = relationship("User", back_populates="chat_members")

class ActionLog(Base):
    __tablename__ = "action_logs"
    id = Column(BigInteger, primary_key=True, index=True)
    bot_id = Column(BigInteger, ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=True)
    user_telegram_id = Column(BigInteger, nullable=True)
    action = Column(String(100))
    reason = Column(String(255), nullable=True)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    bot = relationship("Bot", back_populates="logs")
    chat = relationship("Chat", back_populates="logs")
