import logging
from fastapi import APIRouter, Depends, Path, Request, status
from app.telegram.services.action_log_service import ActionLogService
from app.telegram.services.bot_service import BotService
from app.telegram.services.chat_member_service import ChatMemberService
from app.telegram.services.chat_service import ChatService
from app.telegram.services.user_account_service import UserAccountService
from app.telegram.utils.telegram_util import TelegramUtil
from core.db.pgsql.depend import AsyncSession, get_session

logger = logging.getLogger(__name__)

webhook_router = APIRouter(prefix="/webhook", tags=["Telegram Webhook"])


# ---------- Webhook handler ----------
@webhook_router.post("/callback/{bot_id}", status_code=status.HTTP_200_OK)
async def webhook_handler(
    bot_id: int = Path(..., description="internal bot id"),
    request: Request = None,
    session: AsyncSession = Depends(get_session)
):
    try:
        # Initialize services
        bot_service = BotService(session)
        user_account_service = UserAccountService(session)
        chat_service = ChatService(session)
        chat_member_service = ChatMemberService(session)
        action_log_service = ActionLogService(session)
        telegram_util = TelegramUtil()

        # Parse request body
        body = await request.json()

        bot = await bot_service.get_bot_by_id(bot_id)
        if not bot:
            logger.error("Bot not registered in system")
            return {"ok": True}

        # ---------------------- MESSAGE EVENTS ----------------------
        if "message" in body:
            message = body["message"]
            chat_obj = message.get("chat")
            ch = None

            if chat_obj:
                ch = await chat_service.upsert_chat(bot["id"], chat_obj)

            # Ensure the user who sent the message exists
            from_user = message.get("from")
            if from_user:
                user = await user_account_service.upsert_user(from_user)
                if ch:
                    # Upsert chat member for this user
                    await chat_member_service.upsert_chat_member(
                        bot_id=bot["id"],
                        chat_id=ch["id"],
                        user_id=user["id"],
                        is_bot=user["is_bot"],
                        status="active",
                        role="member"
                    )

                    await action_log_service.log_action(
                        bot["id"],
                        ch["id"],
                        user["telegram_user_id"],
                        "message",
                        payload=str(message)
                    )

            # --- New chat members joined (GROUPS ONLY) ---
            if message.get("new_chat_members"):
                for new_u in message["new_chat_members"]:
                    user = await user_account_service.upsert_user(new_u)
                    if ch:
                        await chat_member_service.upsert_chat_member(
                            bot["id"],
                            ch["id"],
                            user["id"],
                            user["is_bot"],
                            status="active",
                            role="member"
                        )

                        await action_log_service.log_action(
                            bot["id"],
                            ch["id"],
                            new_u.get("id"),
                            "join",
                            payload=str(message)
                        )

            # --- Member left the chat (GROUPS ONLY) ---
            if message.get("left_chat_member"):
                left = message["left_chat_member"]
                user = await user_account_service.upsert_user(left)
                if ch:
                    await chat_member_service.upsert_chat_member(
                        bot["id"],
                        ch["id"],
                        user["id"],
                        user["is_bot"],
                        status="left",
                        role="member"
                    )

                    await action_log_service.log_action(
                        bot["id"],
                        ch["id"],
                        left.get("id"),
                        "left",
                        payload=str(message)
                    )

        # ---------------------- CHAT_MEMBER UPDATES (GROUPS & CHANNELS) ----------------------
        if "chat_member" in body:
            cm = body["chat_member"]
            chat_obj = cm.get("chat")
            new_cm = cm.get("new_chat_member", {})
            old_cm = cm.get("old_chat_member", {})
            user_obj = new_cm.get("user")

            if not user_obj:
                return {"ok": True}  # safety

            ch = None
            if chat_obj:
                ch = await chat_service.upsert_chat(bot["id"], chat_obj)

            user = await user_account_service.upsert_user(user_obj)

            # Map Telegram status → system status
            role, mapped_status, is_muted = telegram_util.map_telegram_member_status(cm)

            # Detect specific events
            old_status = old_cm.get("status")
            new_status = new_cm.get("status")
            chat_type = chat_obj.get("type")  # 'channel', 'group', 'supergroup'

            # *** CHANNEL: Member joined ***
            if chat_type == "channel" and old_status in ["left", "kicked"] and new_status == "member":
                logger.info(f"🎉 CHANNEL JOIN: User {user_obj.get('id')} joined channel {chat_obj.get('title')}")
                action_type = "channel_join"
            
            # *** CHANNEL: Member left ***
            elif chat_type == "channel" and old_status == "member" and new_status in ["left", "kicked"]:
                logger.info(f"👋 CHANNEL LEFT: User {user_obj.get('id')} left channel {chat_obj.get('title')}")
                action_type = "channel_left"
            
            # *** GROUP/SUPERGROUP: Member joined ***
            elif chat_type in ["group", "supergroup"] and old_status in ["left", "kicked"] and new_status == "member":
                logger.info(f"🎉 GROUP JOIN: User {user_obj.get('id')} joined group {chat_obj.get('title')}")
                action_type = "group_join"
            
            # *** GROUP/SUPERGROUP: Member left ***
            elif chat_type in ["group", "supergroup"] and old_status == "member" and new_status in ["left", "kicked"]:
                logger.info(f"👋 GROUP LEFT: User {user_obj.get('id')} left group {chat_obj.get('title')}")
                action_type = "group_left"
            
            # Default: use mapped_status
            else:
                action_type = mapped_status

            await chat_member_service.upsert_chat_member(
                bot["id"],
                ch["id"],
                user["id"],
                user["is_bot"],
                status=mapped_status,
                role=role,
                is_muted=is_muted
            )

            await action_log_service.log_action(
                bot["id"],
                ch["id"],
                user["telegram_user_id"],
                action_type,  # Use specific action type
                payload=str(cm)
            )

        # ---------------------- MY_CHAT_MEMBER (bot's own state) ----------------------
        if "my_chat_member" in body:
            mc = body["my_chat_member"]
            chat_obj = mc.get("chat")
            ch = None

            if chat_obj:
                ch = await chat_service.upsert_chat(bot["id"], chat_obj)

                await action_log_service.log_action(
                    bot["id"],
                    ch["id"],
                    None,
                    "my_chat_member",
                    payload=str(mc)
                )

    except Exception as e:
        logger.error(f"Error from telegram webhook: {e}")
        print("--------------------------------------------------------------------------------------------")
        print("-----------------------------Telegram Body--------------------------------------------------")
        print("--------------------------------------------------------------------------------------------\n")
        print(body)

    return {"ok": True}
