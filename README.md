# DarkGPT

DarkGPT Bot — a Telegram bot that streams uncensored answers via OpenRouter.

## Access control (invite-only)

The bot is invite-only. New people who message the bot are recorded as
**pending** and cannot use it until an **admin approves** them. Approvals are
stored in **Supabase**.

### How it works
- A new user sends `/start` (or any message) → a pending request is created and
  every admin is notified with **Approve / Deny** buttons.
- The admin approves → the user is messaged automatically and can start chatting.
- Admins can also pre-grant access by numeric ID or by `@username` before the
  person ever arrives.

### Admin commands
| Command | What it does |
| --- | --- |
| `/pending` | List pending requests (with Approve/Deny buttons) |
| `/approve <id\|@user>` | Approve a user |
| `/deny <id\|@user>` | Deny a user |
| `/grant <id\|@user>` | Manually add a user (same as approve) |
| `/revoke <id\|@user>` | Remove a user (same as deny) |
| `/users` | List approved users |
| `/stats` | Approved / pending / denied counts |
| `/whoami` | Anyone: show your Telegram ID and status |

## Environment variables (Render)
| Var | Description |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase **service_role** key (server-side only) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs of admins |
| `PORT` | (optional) health-check port, defaults to 8080 |

If `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` are missing, only users listed in
`ADMIN_IDS` can use the bot; everyone else is denied.
