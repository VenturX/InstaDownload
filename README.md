# Instagram Telegram Downloader Bot

A small Telegram bot starter that accepts a **public Instagram post/Reel URL** and tries to send the photos or video back to the user.

## Important limitations

- This is a starter project, not a guarantee that Instagram extraction will always work. Instagram regularly changes its pages and may block requests from datacenter IP addresses.
- It does not log in to Instagram, access private accounts, bypass access controls, or accept Instagram passwords/cookies.
- Use it only for media you own or are authorized to download. Respect copyright and the platform's terms.
- Telegram bot uploads are limited here to about 48 MB per file; large files are skipped.
- The project is intentionally limited to one simultaneous download to fit a small free server.

## 1. Create the Telegram bot

1. Open Telegram and chat with `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the bot token. Keep it private; do not put it in source code or commit it to GitHub.

## 2. Deploy on Koyeb's free web service

Koyeb's free instance is small and scales to zero after inactivity, so the bot may be slow to respond after it has been idle. It is suitable for testing and low traffic, not guaranteed always-on production use.

1. Create a GitHub repository and upload these project files.
2. In Koyeb, create a **Web Service** from that GitHub repository.
3. Use the Python buildpack (or Docker if you prefer).
4. Set the run command to:

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

5. Configure the service to expose port `8000` as HTTP.
6. Add these environment variables:

   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | Token from BotFather |
   | `PORT` | `8000` |
   | `WEBHOOK_SECRET` | A long random string using only A-Z, a-z, 0-9, `_`, `-` |
   | `PUBLIC_BASE_URL` | Leave empty on the first deploy if you do not yet know the service URL |

7. Deploy. Copy the public HTTPS service URL shown by Koyeb, such as `https://your-service-name.koyeb.app`.
8. Set `PUBLIC_BASE_URL` to that exact HTTPS URL (without a trailing slash), then redeploy.
9. Open `https://your-service-name.koyeb.app/healthz`. It should return `{"status":"healthy"}`.
10. Open your Telegram bot and send `/start`, then try a public post/Reel URL.

The app registers its Telegram webhook automatically when it starts and `PUBLIC_BASE_URL` is set.

## 3. Run locally for testing

You still need a public HTTPS address for Telegram webhooks. For local development, you can use a temporary HTTPS tunnel (for example, a tunnel service) and set `PUBLIC_BASE_URL` to its URL. Do not expose your bot token in logs or screenshots.

Install dependencies and run:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
$env:BOT_TOKEN="YOUR_BOT_TOKEN"
$env:PUBLIC_BASE_URL="https://YOUR_PUBLIC_HTTPS_URL"
$env:WEBHOOK_SECRET="REPLACE_WITH_RANDOM_SECRET"
uvicorn main:app --host 0.0.0.0 --port 8000
```

On Linux/macOS, use `export NAME=value` instead of PowerShell's `$env:NAME=value` syntax.

## Files

- `main.py` — Telegram handlers, Instagram URL validation, download logic, and webhook server.
- `requirements.txt` — Python packages.

## Troubleshooting

- **Bot does not respond:** check Koyeb deployment logs, confirm `BOT_TOKEN`, and verify `PUBLIC_BASE_URL` exactly matches the public HTTPS service URL.
- **Instagram says login required or returns no media:** the server may be blocked or Instagram may have changed its extraction flow. This starter intentionally does not store or use personal Instagram cookies.
- **Only part of a carousel arrives:** try updating `yt-dlp` and `gallery-dl`; extractor support can change between releases.
- **File too large:** this starter skips files above 48 MB. The standard Telegram Bot API currently documents a 50 MB upload limit for videos/documents.
