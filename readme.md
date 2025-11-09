### Run
```
uvicorn main:app --reload
```


```
✅ Clean Database Design
python# chat_members table
- role: "creator" | "administrator" | "member" | "restricted" | "left" | "kicked"
- status: "member" | "left" | "banned" | "restricted"
- is_bot: True/False (independent attribute)
- is_muted: True/False (independent restriction)
Perfect separation of concerns:

role = What they CAN do (permissions)
status = Whether they're IN or OUT
is_bot = Type of entity
is_muted = Communication restriction

✅ Complete Features
Your system can:

✅ Register bots and discover all groups
✅ Connect/disconnect webhooks
✅ Get all users in each group with badges
✅ Ban/kick/mute users
✅ Track joins and leaves automatically
✅ Store everything in PostgreSQL with proper relationships
✅ Show bot roles correctly (bot member, bot admin, bot owner)
✅ Return badge-ready data for frontend

🎯 Ready to Use!
Your API endpoints:
bashPOST /bots/register                                    # Register bot
POST /bots/{bot_id}/connect                           # Set webhook
POST /bots/{bot_id}/disconnect                        # Delete webhook
GET  /bots/{bot_id}/webhook-status                    # Check connection
POST /bots/{bot_id}/sync-all-chats                    # Refresh groups
GET  /bots/{bot_id}/chats                             # List all groups
GET  /bots/{bot_id}/chats/{chat_id}/members           # List members with badges
POST /bots/{bot_id}/chats/{chat_id}/sync-members      # Sync members
POST /bots/{bot_id}/ban                               # Ban user
POST /bots/{bot_id}/unban                             # Unban user
POST /bots/{bot_id}/kick                              # Kick user
POST /bots/{bot_id}/mute                              # Mute user
POST /bots/{bot_id}/unmute                            # Unmute user
POST /webhook/{bot_id}                                 # Webhook receiver
```
