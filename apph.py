from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import httpx
import uvicorn
from typing import Optional
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
import asyncio
from contextlib import asynccontextmanager
# telethon
# Configuration - will be set via API
BOT_TOKEN = ""
WEBHOOK_URL = ""
TELEGRAM_API = ""

# MTProto Configuration - will be set via API
API_ID = 0
API_HASH = ""
SESSION_STRING = ""

# Config files for persistence
SESSION_FILE = "telegram_session.txt"
CONFIG_FILE = "telegram_config.json"

# Global MTProto client
mtproto_client: Optional[TelegramClient] = None


# ============================================================================
# LIFESPAN EVENT HANDLER (AUTO-LOAD SESSION)
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event handler - runs on startup and shutdown
    Automatically loads saved session when server starts
    """
    global mtproto_client, SESSION_STRING, API_ID, API_HASH

    import os
    import json

    # STARTUP
    print("🚀 Server starting up...")

    # Load API credentials from config file
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                API_ID = config.get('api_id', 0)
                API_HASH = config.get('api_hash', '')
            print(f"✅ Loaded API credentials from {CONFIG_FILE}")
        except Exception as e:
            print(f"⚠️  Could not load config: {e}")

    # Try to load session from file
    if os.path.exists(SESSION_FILE):
        print(f"📁 Found session file: {SESSION_FILE}")

        try:
            with open(SESSION_FILE, 'r') as f:
                saved_session = f.read().strip()

            if saved_session and API_ID and API_HASH:
                print("🔄 Attempting to load saved session...")
                SESSION_STRING = saved_session

                client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
                await client.connect()

                if await client.is_user_authorized():
                    mtproto_client = client
                    me = await client.get_me()
                    print(f"✅ MTProto session loaded successfully!")
                    print(f"   Logged in as: {me.first_name} (@{me.username})")
                else:
                    print("⚠️  Saved session expired")
                    print("   Call /setup-mtproto to re-authenticate")
            elif saved_session and not (API_ID and API_HASH):
                print("⚠️  Session file found, but API_ID/API_HASH not set")
                print("   Call /setup-mtproto to configure and authenticate")
        except Exception as e:
            print(f"❌ Could not load session: {e}")
            print("   Call /setup-mtproto to re-authenticate")
    else:
        print(f"ℹ️  No session file found ({SESSION_FILE})")
        print("   Call /setup-mtproto to authenticate for the first time")

    yield  # Server runs here

    # SHUTDOWN
    print("🛑 Server shutting down...")
    if mtproto_client:
        await mtproto_client.disconnect()
        print("✅ MTProto client disconnected")


app = FastAPI(title="Telegram Channel Manager Bot", lifespan=lifespan)


class WebhookSetup(BaseModel):
    token: str
    webhook_url: Optional[str] = None


class BanUserRequest(BaseModel):
    chat_id: int | str
    user_id: int
    until_date: Optional[int] = None
    revoke_messages: Optional[bool] = False


class MTProtoSetup(BaseModel):
    api_id: int
    api_hash: str
    phone: str  # Phone number for authentication


class ChannelActionRequest(BaseModel):
    channel_id: int | str
    user_id: int
    action: str  # "ban", "unban", "remove", "mute", "unmute"
    reason: Optional[str] = None
    until_date: Optional[int] = None  # Unix timestamp for temporary restrictions


@app.post("/channel/manage-user")
async def manage_channel_user(data: ChannelActionRequest):
    """
    Manage a user in a channel - ban, unban, remove, mute, or unmute

    POST /channel/manage-user
    Body: {
        "channel_id": "@channelname" or 3587458353 or -1003587458353,
        "user_id": 123456789,
        "action": "ban" | "unban" | "remove" | "mute" | "unmute",
        "reason": "Spam" (optional),
        "until_date": 1735689600 (optional, Unix timestamp for temporary action)
    }

    Actions:
    - ban: Permanently ban user (can't view or join)
    - unban: Remove ban (user can join again)
    - remove: Just remove from channel (user can join back)
    - mute: Restrict user from posting (can still view)
    - unmute: Allow user to post again

    REQUIRES: MTProto client to be set up and authenticated
    """
    if not mtproto_client or not await mtproto_client.is_user_authorized():
        raise HTTPException(
            status_code=401,
            detail="MTProto client not authenticated. Call /setup-mtproto first"
        )

    try:
        from telethon.tl.types import ChatBannedRights

        # Format channel ID properly
        formatted_channel_id = format_channel_id(data.channel_id)

        # Get channel and user entities
        channel = await mtproto_client.get_entity(formatted_channel_id)
        user = await mtproto_client.get_entity(data.user_id)

        action_result = ""

        if data.action == "ban":
            # Permanent ban - can't view or join
            banned_rights = ChatBannedRights(
                until_date=data.until_date,  # None = permanent, or Unix timestamp
                view_messages=True,  # Can't view
                send_messages=True,
                send_media=True,
                send_stickers=True,
                send_gifs=True,
                send_games=True,
                send_inline=True,
                embed_links=True,
                send_polls=True,
                change_info=True,
                invite_users=True,
                pin_messages=True
            )

            await mtproto_client(functions.channels.EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=banned_rights
            ))

            if data.until_date:
                action_result = f"User banned until timestamp {data.until_date}"
            else:
                action_result = "User permanently banned from channel"

        elif data.action == "unban":
            # Remove all restrictions
            unbanned_rights = ChatBannedRights(
                until_date=None,
                view_messages=False,
                send_messages=False,
                send_media=False,
                send_stickers=False,
                send_gifs=False,
                send_games=False,
                send_inline=False,
                embed_links=False
            )

            await mtproto_client(functions.channels.EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=unbanned_rights
            ))

            action_result = "User unbanned from channel"

        elif data.action == "remove":
            # Just kick from channel (can rejoin)
            from telethon.tl.functions.channels import EditBannedRequest

            # Kick by banning then unbanning immediately
            kick_rights = ChatBannedRights(
                until_date=None,
                view_messages=True
            )

            await mtproto_client(EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=kick_rights
            ))

            # Immediately unban so they can rejoin
            unban_rights = ChatBannedRights(
                until_date=None,
                view_messages=False
            )

            await mtproto_client(EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=unban_rights
            ))

            action_result = "User removed from channel (can rejoin)"

        elif data.action == "mute":
            # Mute - can view but can't post
            muted_rights = ChatBannedRights(
                until_date=data.until_date,  # None = permanent, or Unix timestamp
                view_messages=False,  # Can still view
                send_messages=True,   # Can't send messages
                send_media=True,
                send_stickers=True,
                send_gifs=True,
                send_games=True,
                send_inline=True,
                embed_links=True,
                send_polls=True
            )

            await mtproto_client(functions.channels.EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=muted_rights
            ))

            if data.until_date:
                action_result = f"User muted until timestamp {data.until_date}"
            else:
                action_result = "User permanently muted (can view, can't post)"

        elif data.action == "unmute":
            # Remove message restrictions (same as unban)
            unmuted_rights = ChatBannedRights(
                until_date=None,
                view_messages=False,
                send_messages=False,
                send_media=False,
                send_stickers=False,
                send_gifs=False,
                send_games=False,
                send_inline=False,
                embed_links=False,
                send_polls=False
            )

            await mtproto_client(functions.channels.EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=unmuted_rights
            ))

            action_result = "User unmuted (can post again)"

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid action: {data.action}. Use: ban, unban, remove, mute, or unmute"
            )

        return {
            "status": "success",
            "action": data.action,
            "message": action_result,
            "user": {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name
            },
            "channel": {
                "id": channel.id,
                "title": channel.title
            },
            "reason": data.reason
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to {data.action} user: {str(e)}")


# ============================================================================
# MTPROTO CLIENT SETUP
# ============================================================================

@app.post("/setup-mtproto")
async def setup_mtproto(data: MTProtoSetup):
    """
    Setup MTProto client for channel subscriber management
    POST /setup-mtproto
    Body: {
        "api_id": 12345678,
        "api_hash": "your_hash",
        "phone": "+1234567890"
    }

    API credentials will be saved to telegram_config.json for auto-load on restart.
    This will send you a code via Telegram.
    Call /verify-mtproto with the code to complete setup.
    """
    global API_ID, API_HASH, mtproto_client

    API_ID = data.api_id
    API_HASH = data.api_hash

    try:
        # Save API credentials to config file
        import json
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                'api_id': API_ID,
                'api_hash': API_HASH
            }, f)

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()

        # Send code request
        await client.send_code_request(data.phone)

        # Store client temporarily
        mtproto_client = client

        return {
            "status": "success",
            "message": "Code sent to your Telegram account",
            "config_saved": CONFIG_FILE,
            "next_step": "Call /verify-mtproto with the code you received"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/verify-mtproto")
async def verify_mtproto(phone: str, code: str, password: Optional[str] = None):
    """
    Verify the code received via Telegram
    POST /verify-mtproto?phone=+1234567890&code=12345

    If you have 2FA enabled, also provide password parameter.
    Session will be saved to telegram_session.txt for auto-reconnect.
    """
    global mtproto_client, SESSION_STRING

    if not mtproto_client:
        raise HTTPException(status_code=400, detail="Call /setup-mtproto first")

    try:
        # Sign in with code
        await mtproto_client.sign_in(phone, code, password=password)

        # Save session string
        SESSION_STRING = mtproto_client.session.save()

        # Save to file for persistence
        with open(SESSION_FILE, 'w') as f:
            f.write(SESSION_STRING)

        # Get account info
        me = await mtproto_client.get_me()

        return {
            "status": "success",
            "message": "MTProto client authenticated successfully",
            "user": {
                "id": me.id,
                "username": me.username,
                "phone": me.phone
            },
            "session_string": SESSION_STRING,
            "session_file": SESSION_FILE,
            "note": f"Session saved to {SESSION_FILE}. Will auto-load on next startup."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/load-session")
async def load_session(session_string: str):
    """
    Load existing session without re-authenticating
    POST /load-session
    Body: {"session_string": "your_saved_session"}
    """
    global mtproto_client, SESSION_STRING

    try:
        SESSION_STRING = session_string
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="Session expired, please re-authenticate")

        mtproto_client = client
        me = await client.get_me()

        return {
            "status": "success",
            "message": "Session loaded successfully",
            "user": {
                "id": me.id,
                "username": me.username
            }
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# CHANNEL SUBSCRIBER MANAGEMENT (MTPROTO)
# ============================================================================

@app.post("/channel/ban-subscriber")
async def ban_channel_subscriber(channel_id: int | str, user_id: int, reason: Optional[str] = None):
    """
    DEPRECATED: Use /channel/manage-user instead

    Ban a subscriber from a channel

    POST /channel/ban-subscriber?channel_id=3587458353&user_id=123456789&reason=Spam
    """
    # Redirect to new unified endpoint
    data = ChannelActionRequest(
        channel_id=channel_id,
        user_id=user_id,
        action="ban",
        reason=reason
    )
    return await manage_channel_user(data)


@app.post("/channel/unban-subscriber")
async def unban_channel_subscriber_old(channel_id: int | str, user_id: int):
    """
    DEPRECATED: Use /channel/manage-user instead

    Unban a subscriber from a channel
    POST /channel/unban-subscriber?channel_id=3587458353&user_id=123456789
    """
    # Redirect to new unified endpoint
    data = ChannelActionRequest(
        channel_id=channel_id,
        user_id=user_id,
        action="unban"
    )
    return await manage_channel_user(data)


@app.get("/channel/get-all")
async def get_all_channels(only_manageable: bool = True):
    """
    Get all channels and groups that the authenticated user is part of
    GET /channel/get-all?only_manageable=true

    Parameters:
    - only_manageable: If true (default), only return channels where you can ban users
                       (creator or admin with ban_users permission)

    Returns channels where you have permission to manage subscribers
    """
    if not mtproto_client or not await mtproto_client.is_user_authorized():
        raise HTTPException(status_code=401, detail="MTProto client not authenticated")

    try:
        from telethon.tl.types import Channel, Chat
        from telethon.tl.functions.channels import GetParticipantRequest

        dialogs = await mtproto_client.get_dialogs()

        channels = []
        groups = []

        # Get current user ID
        me = await mtproto_client.get_me()
        my_user_id = me.id

        for dialog in dialogs:
            entity = dialog.entity

            if isinstance(entity, Channel):
                channel_info = {
                    "id": entity.id,
                    "title": entity.title,
                    "username": entity.username,
                    "participants_count": getattr(entity, 'participants_count', 0),
                    "is_broadcast": entity.broadcast,
                    "is_megagroup": entity.megagroup,
                    "access_hash": entity.access_hash,
                    "date": entity.date.isoformat() if hasattr(entity.date, 'isoformat') else str(entity.date)
                }

                # Check permissions - Try multiple methods
                can_manage = False
                is_creator = False
                is_admin = False
                can_ban = False

                try:
                    # Method 1: Get permissions (fast but sometimes inaccurate)
                    participant = await mtproto_client.get_permissions(entity)
                    is_admin = participant.is_admin
                    is_creator = participant.is_creator
                    can_ban = participant.ban_users
                    can_manage = is_creator or can_ban
                except:
                    pass

                # Method 2: If not detected as creator, check via GetParticipant (more accurate)
                if not can_manage:
                    try:
                        result = await mtproto_client(GetParticipantRequest(
                            channel=entity,
                            participant=my_user_id
                        ))

                        from telethon.tl.types import ChannelParticipantCreator, ChannelParticipantAdmin

                        if isinstance(result.participant, ChannelParticipantCreator):
                            is_creator = True
                            can_manage = True
                            can_ban = True
                        elif isinstance(result.participant, ChannelParticipantAdmin):
                            is_admin = True
                            admin_rights = result.participant.admin_rights
                            can_ban = admin_rights.ban_users if admin_rights else False
                            can_manage = can_ban
                    except Exception as e:
                        # If GetParticipant fails, might still be creator
                        # (happens with some channels)
                        pass

                channel_info["is_admin"] = is_admin
                channel_info["is_creator"] = is_creator
                channel_info["can_ban_users"] = can_ban

                # Skip if filtering and user can't manage
                if only_manageable and not can_manage:
                    continue

                if entity.broadcast:
                    channels.append(channel_info)
                else:
                    groups.append(channel_info)

            elif isinstance(entity, Chat):
                # For regular groups
                group_info = {
                    "id": entity.id,
                    "title": entity.title,
                    "participants_count": getattr(entity, 'participants_count', 0),
                    "is_broadcast": False,
                    "is_megagroup": False,
                    "date": entity.date.isoformat() if hasattr(entity.date, 'isoformat') else str(entity.date)
                }
                groups.append(group_info)

        return {
            "status": "success",
            "summary": {
                "total_channels": len(channels),
                "total_groups": len(groups),
                "total": len(channels) + len(groups),
                "filtered": "Only showing manageable channels/groups" if only_manageable else "Showing all channels/groups"
            },
            "channels": channels,
            "groups": groups
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get channels: {str(e)}")


@app.get("/channel/get-info")
async def get_channel_info(channel_id: int | str):
    """
    Get detailed information about a specific channel
    GET /channel/get-info?channel_id=@channel or 3587458353 or -1003587458353
    """
    if not mtproto_client or not await mtproto_client.is_user_authorized():
        raise HTTPException(status_code=401, detail="MTProto client not authenticated")

    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.types import ChannelParticipantCreator, ChannelParticipantAdmin

        # Format channel ID properly
        formatted_id = format_channel_id(channel_id)
        channel = await mtproto_client.get_entity(formatted_id)
        full_channel = await mtproto_client(GetFullChannelRequest(channel))

        # Get your permissions - use multiple methods
        permissions_info = {
            "is_admin": False,
            "is_creator": False,
            "can_post_messages": False,
            "can_edit_messages": False,
            "can_delete_messages": False,
            "can_ban_users": False,
            "can_invite_users": False,
            "can_pin_messages": False,
            "can_add_admins": False
        }

        try:
            # Method 1: Try get_permissions
            permissions = await mtproto_client.get_permissions(channel)
            if permissions:
                permissions_info = {
                    "is_admin": permissions.is_admin,
                    "is_creator": permissions.is_creator,
                    "can_post_messages": permissions.post_messages,
                    "can_edit_messages": permissions.edit_messages,
                    "can_delete_messages": permissions.delete_messages,
                    "can_ban_users": permissions.ban_users,
                    "can_invite_users": permissions.invite_users,
                    "can_pin_messages": permissions.pin_messages,
                    "can_add_admins": permissions.add_admins
                }
        except:
            pass

        # Method 2: If permissions are still False, try GetParticipant
        if not permissions_info["is_admin"] and not permissions_info["is_creator"]:
            try:
                me = await mtproto_client.get_me()
                participant_info = await mtproto_client(GetParticipantRequest(
                    channel=channel,
                    participant=me.id
                ))

                if isinstance(participant_info.participant, ChannelParticipantCreator):
                    permissions_info["is_creator"] = True
                    permissions_info["can_post_messages"] = True
                    permissions_info["can_edit_messages"] = True
                    permissions_info["can_delete_messages"] = True
                    permissions_info["can_ban_users"] = True
                    permissions_info["can_invite_users"] = True
                    permissions_info["can_pin_messages"] = True
                    permissions_info["can_add_admins"] = True

                elif isinstance(participant_info.participant, ChannelParticipantAdmin):
                    permissions_info["is_admin"] = True
                    admin_rights = participant_info.participant.admin_rights
                    if admin_rights:
                        permissions_info["can_post_messages"] = admin_rights.post_messages
                        permissions_info["can_edit_messages"] = admin_rights.edit_messages
                        permissions_info["can_delete_messages"] = admin_rights.delete_messages
                        permissions_info["can_ban_users"] = admin_rights.ban_users
                        permissions_info["can_invite_users"] = admin_rights.invite_users
                        permissions_info["can_pin_messages"] = admin_rights.pin_messages
                        permissions_info["can_add_admins"] = admin_rights.add_admins
            except:
                pass

        return {
            "status": "success",
            "channel": {
                "id": channel.id,
                "title": channel.title,
                "username": channel.username,
                "description": full_channel.full_chat.about,
                "participants_count": full_channel.full_chat.participants_count,
                "is_broadcast": channel.broadcast,
                "is_megagroup": channel.megagroup,
                "created_date": channel.date.isoformat() if hasattr(channel.date, 'isoformat') else str(channel.date)
            },
            "your_permissions": permissions_info
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get channel info: {str(e)}")


def format_channel_id(channel_id: int | str) -> int:
    """
    Convert channel ID to proper format
    Telegram channel IDs need -100 prefix
    """
    if isinstance(channel_id, str):
        # If it's a username like @channel, return as is
        if channel_id.startswith('@'):
            return channel_id
        # Try to convert to int
        try:
            channel_id = int(channel_id)
        except:
            return channel_id

    # If it's a positive number, add -100 prefix
    if isinstance(channel_id, int) and channel_id > 0:
        return -1000000000000 - channel_id

    return channel_id


@app.get("/channel/get-subscribers")
async def get_channel_subscribers(channel_id: int | str, limit: int = 100, offset: int = 0):
    """
    Get list of channel subscribers with their roles
    GET /channel/get-subscribers?channel_id=@channel&limit=100&offset=0
    GET /channel/get-subscribers?channel_id=3587458353&limit=100&offset=0
    GET /channel/get-subscribers?channel_id=-1003587458353&limit=100&offset=0

    Note: Only works if you're the channel owner/admin
    Offset parameter will skip the first N users for pagination
    """
    if not mtproto_client or not await mtproto_client.is_user_authorized():
        raise HTTPException(status_code=401, detail="MTProto client not authenticated")

    try:
        from telethon.tl.types import (
            ChannelParticipantCreator,
            ChannelParticipantAdmin,
            ChannelParticipant,
            ChannelParticipantSelf
        )

        # Format channel ID properly
        formatted_id = format_channel_id(channel_id)
        channel = await mtproto_client.get_entity(formatted_id)

        subscribers = []
        count = 0

        # Get participants with their role info
        async for participant in mtproto_client.iter_participants(
            channel,
            limit=limit + offset,
            aggressive=True  # Get full participant info including admin rights
        ):
            # Skip first 'offset' users
            if count < offset:
                count += 1
                continue

            user = participant

            # Determine role
            role = "member"
            admin_title = None
            permissions = {}

            # Try to get detailed participant info
            try:
                from telethon.tl.functions.channels import GetParticipantRequest
                participant_info = await mtproto_client(GetParticipantRequest(
                    channel=channel,
                    participant=user.id
                ))

                if isinstance(participant_info.participant, ChannelParticipantCreator):
                    role = "creator"
                    admin_title = "Owner"
                elif isinstance(participant_info.participant, ChannelParticipantAdmin):
                    role = "admin"
                    admin_rights = participant_info.participant.admin_rights
                    admin_title = participant_info.participant.rank or "Admin"

                    if admin_rights:
                        permissions = {
                            "can_change_info": admin_rights.change_info,
                            "can_post_messages": admin_rights.post_messages,
                            "can_edit_messages": admin_rights.edit_messages,
                            "can_delete_messages": admin_rights.delete_messages,
                            "can_ban_users": admin_rights.ban_users,
                            "can_invite_users": admin_rights.invite_users,
                            "can_pin_messages": admin_rights.pin_messages,
                            "can_add_admins": admin_rights.add_admins,
                            "can_manage_call": admin_rights.manage_call
                        }
            except:
                # If we can't get detailed info, just mark as member
                pass

            subscriber_info = {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone if hasattr(user, 'phone') else None,
                "is_bot": user.bot,
                "is_verified": user.verified if hasattr(user, 'verified') else False,
                "is_scam": user.scam if hasattr(user, 'scam') else False,
                "role": role,  # creator, admin, or member
                "admin_title": admin_title  # Owner, Admin, or custom title
            }

            # Add permissions only for admins
            if permissions:
                subscriber_info["permissions"] = permissions

            subscribers.append(subscriber_info)

            count += 1

            # Stop when we have enough subscribers
            if len(subscribers) >= limit:
                break

        return {
            "status": "success",
            "channel": {
                "id": channel.id,
                "title": channel.title,
                "username": channel.username
            },
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(subscribers)
            },
            "subscribers": subscribers
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# AUTOMATED MODERATION (FLAG SYSTEM)
# ============================================================================

# Store flagged users - tracks reports and auto-bans after threshold
flagged_users = {}

@app.post("/flag-user")
async def flag_user(user_id: int, reason: str, channel_id: int | str):
    """
    Flag a user for review/auto-ban (Automated Moderation System)

    POST /flag-user
    Body: {
        "user_id": 123456789,
        "reason": "spam",
        "channel_id": "@channel" or 3587458353
    }

    How it works:
    - Each flag increases the user's report count
    - After 3 reports, user is automatically banned from the channel
    - Useful for community-driven moderation or spam detection

    Use cases:
    - Multiple users report the same spammer
    - Your bot detects spam patterns and flags automatically
    - Build a reputation system
    """
    if user_id not in flagged_users:
        flagged_users[user_id] = {
            "reports": 0,
            "reasons": [],
            "channels": []
        }

    flagged_users[user_id]["reports"] += 1
    flagged_users[user_id]["reasons"].append(reason)
    flagged_users[user_id]["channels"].append(channel_id)

    report_count = flagged_users[user_id]["reports"]

    # Auto-ban if threshold reached (3 reports)
    if report_count >= 3:
        try:
            # Ban from all reported channels
            for ch_id in set(flagged_users[user_id]["channels"]):
                formatted_id = format_channel_id(ch_id)
                await manage_channel_user(ChannelActionRequest(
                    channel_id=formatted_id,
                    user_id=user_id,
                    action="ban",
                    reason=f"Auto-banned: {', '.join(set(flagged_users[user_id]['reasons']))}"
                ))

            return {
                "status": "auto_banned",
                "message": f"User {user_id} auto-banned after {report_count} reports",
                "reports": report_count,
                "reasons": list(set(flagged_users[user_id]["reasons"])),
                "channels_banned_from": list(set(flagged_users[user_id]["channels"]))
            }
        except Exception as e:
            return {
                "status": "flagged",
                "message": f"User flagged but auto-ban failed: {str(e)}",
                "reports": report_count,
                "threshold": "3 reports needed"
            }

    return {
        "status": "flagged",
        "reports": report_count,
        "threshold_remaining": 3 - report_count,
        "message": f"User flagged. {3 - report_count} more report(s) needed for auto-ban"
    }


@app.get("/flagged-users")
async def get_flagged_users():
    """
    Get list of all flagged users and their report counts

    GET /flagged-users

    Returns all users who have been flagged, their report counts,
    reasons, and which channels they were reported in.
    """
    return {
        "status": "success",
        "total_flagged": len(flagged_users),
        "flagged_users": flagged_users
    }


@app.post("/clear-flags")
async def clear_user_flags(user_id: int):
    """
    Clear all flags for a specific user

    POST /clear-flags?user_id=123456789

    Use this to forgive a user or reset their report count
    """
    if user_id in flagged_users:
        del flagged_users[user_id]
        return {
            "status": "success",
            "message": f"Cleared all flags for user {user_id}"
        }
    else:
        return {
            "status": "not_found",
            "message": f"User {user_id} has no flags"
        }


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def send_telegram_message(text: str, chat_id: Optional[int] = None):
    """Send notification via bot"""
    if not chat_id:
        # Send to yourself (get from /getMe)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{TELEGRAM_API}/getMe")
            result = response.json()
            if result.get("ok"):
                chat_id = result["result"]["id"]

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )


# ============================================================================
# ORIGINAL BOT API ENDPOINTS (Keep all your existing endpoints)
# ============================================================================

@app.post("/setup-webhook")
async def setup_webhook(data: WebhookSetup):
    global BOT_TOKEN, TELEGRAM_API, WEBHOOK_URL
    BOT_TOKEN = data.token
    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    if data.webhook_url:
        WEBHOOK_URL = data.webhook_url

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": WEBHOOK_URL,
                "allowed_updates": ["message", "chat_member", "my_chat_member"]
            }
        )
        result = response.json()
        if result.get("ok"):
            return {
                "status": "success",
                "message": "Webhook set successfully",
                "webhook_url": WEBHOOK_URL,
                "result": result
            }
        else:
            raise HTTPException(status_code=400, detail=result)


@app.post("/ban-user")
async def ban_user(data: BanUserRequest):
    """Ban using Bot API (for groups/supergroups where bot is admin)"""
    if not BOT_TOKEN:
        raise HTTPException(status_code=400, detail="Bot token not configured. Call /setup-webhook first.")

    until_date = data.until_date if data.until_date and data.until_date > 0 else None

    async with httpx.AsyncClient() as client:
        payload = {
            "chat_id": data.chat_id,
            "user_id": data.user_id,
            "revoke_messages": data.revoke_messages
        }
        if until_date:
            payload["until_date"] = until_date

        response = await client.post(
            f"{TELEGRAM_API}/banChatMember",
            json=payload
        )
        result = response.json()

        if result.get("ok"):
            ban_type = "permanently" if not until_date else f"until {until_date}"
            return {
                "status": "success",
                "message": f"User {data.user_id} banned {ban_type}",
                "result": result
            }
        else:
            raise HTTPException(status_code=400, detail=result)


@app.post("/webhook")
async def webhook_handler(request: Request):
    """Handle Telegram updates"""
    try:
        update = await request.json()
        print("="*80)
        print("📨 NEW UPDATE")
        print("="*80)

        if "message" in update:
            message = update["message"]
            text = message.get("text", "")
            user_id = message.get("from", {}).get("id")

            # Auto-flag spam messages
            if any(spam_word in text.lower() for spam_word in ["buy now", "click here", "free money"]):
                channel_id = message.get("chat", {}).get("id")
                await flag_user(user_id, "spam_detected", channel_id)

        return {"status": "ok"}
    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/")
async def root():
    return {
        "message": "Telegram Channel Manager Bot",
        "status": {
            "bot_configured": bool(BOT_TOKEN),
            "mtproto_configured": bool(API_ID and API_HASH),
            "mtproto_connected": bool(mtproto_client)
        },
        "features": [
            "✅ Ban/unban users from channels (MTProto)",
            "✅ Get channel subscribers list",
            "✅ Auto-flag and ban spam users",
            "✅ Traditional bot API for groups"
        ],
        "setup_steps": [
            "1. POST /setup-webhook with bot_token and webhook_url",
            "2. Get API credentials from https://my.telegram.org",
            "3. POST /setup-mtproto with api_id, api_hash, phone",
            "4. POST /verify-mtproto with the code you receive",
            "5. Now you can use all channel management endpoints"
        ],
        "endpoints": {
            "Configuration": {
                "POST /setup-webhook": "Configure bot token and webhook URL"
            },
            "MTProto (Channel Management)": {
                "POST /setup-mtproto": "Initial setup with phone number",
                "POST /verify-mtproto": "Verify with code from Telegram",
                "POST /load-session": "Load saved session",
                "GET /session-status": "Check if MTProto is connected",
                "GET /channel/get-all": "Get all channels/groups you're in",
                "GET /channel/get-info": "Get detailed channel information",
                "GET /channel/get-subscribers": "Get subscriber list with roles (paginated)",
                "POST /channel/manage-user": "Ban/Unban/Remove/Mute/Unmute user (recommended)",
                "POST /channel/ban-subscriber": "Ban user (deprecated, use manage-user)",
                "POST /channel/unban-subscriber": "Unban user (deprecated, use manage-user)"
            },
            "Moderation": {
                "POST /flag-user": "Flag user for review",
                "GET /flagged-users": "Get all flagged users"
            },
            "Bot API (Groups)": {
                "POST /ban-user": "Ban using bot (for groups)",
                "GET /webhook-info": "Get webhook status",
                "GET /get-me": "Get bot info",
                "GET /get-chat": "Get chat info"
            }
        }
    }


if __name__ == "__main__":
    print("🚀 Starting Telegram Channel Manager...")
    print("\n📋 Configuration via API:")
    print("   1. POST /setup-webhook - Configure bot token")
    print("   2. POST /setup-mtproto - Configure API credentials")
    print("   3. POST /verify-mtproto - Complete authentication")
    print("\n🌐 Server starting on http://0.0.0.0:8000")

    uvicorn.run(app, host="0.0.0.0", port=8000)
