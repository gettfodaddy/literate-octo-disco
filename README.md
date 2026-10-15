# CDN Access Telegram Bot

Private Telegram bot for requesting BeelineCDN and TurboFlare access links.

## Login routes

| Menu route | Credential/token source | Login destination |
|---|---|---|
| CDNVideo → Beeline CDN | api.cdnvideo.ru | cdn.beeline.ru |
| CDNVideo → Turboflare | api.cdnvideo.ru | dashboard.turboflare.ru |
| Turboflare → Beeline CDN | dashboard.turboflare.ru | cdn.beeline.ru |
| Turboflare → CDNVideo | dashboard.turboflare.ru | dashboard.cdnvideo.ru |
| CDNVideo → Platformcraft | api.cdnvideo.ru | global.platformcraft.com |
| Turboflare → Platformcraft | dashboard.turboflare.ru | global.platformcraft.com |

Platformcraft routes use the source token unchanged in the login URL; acceptance by Platformcraft is unverified.

The menu shows three paired rows: CDNVideo routes on the left and Turboflare routes on the right. Direct Turboflare login is hidden; its legacy callback remains compatible with older messages. The legacy callback `service:beeline`
now explicitly identifies CDNVideo as the credential source.

The bot authenticates an existing account at the source and builds a target
login URL. It does not register accounts, activate services, or verify that the
target accepts the token. Cross-service login and CDN delivery must be tested
separately. Enter credentials belonging to the displayed source service.

## Security behavior

- The bot refuses to start without an explicit Telegram user ID allowlist.
- It accepts the flow only in private chats and checks access at each step.
- Password messages are deleted before the outbound request when Telegram permits it. The password is not written to logs or files.
- API response bodies are never returned to the chat; error messages are generic.
- Network retry is limited to connection-establishment failures. Read/write timeouts are not retried because the service may already have processed the request.
- Failed authentication attempts are rate-limited in memory. The counters reset when the bot process restarts.
- A successful login URL contains a bearer token. It is sent as an inline button, never written to logs, and can be removed from the chat with the delete button.

**Important limitation:** this preserves the credential exchange used by the supplied script (`username` + `password` sent to the service token endpoint over HTTPS). The password still passes through Telegram before deletion. A browser-based OAuth/PKCE flow would be preferable, but the supplied source does not provide the provider's authorization-code flow details, so this project does not invent one.

## Requirements

- Python 3.10 or newer
- A Telegram bot token from BotFather
- Your numeric Telegram user ID
- An account with the target CDN service

## Configure and run

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='token-from-BotFather'
export ALLOWED_TELEGRAM_USER_IDS='123456789'
python cdn_telegram_bot.py
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` and set variables with `$env:TELEGRAM_BOT_TOKEN = '...'` and `$env:ALLOWED_TELEGRAM_USER_IDS = '123456789'`.

For multiple trusted users, list IDs separated by commas. Do not commit `.env` files or paste the bot token into chat. Run one bot process at a time; the rate-limit counters are process-local.

The service URLs and expected response fields are inherited from the supplied script and have not been independently verified against provider documentation.
