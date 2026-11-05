from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


# ----------------------------- Configuration -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
MAX_FILE_BYTES = 48 * 1024 * 1024  # Telegram bot upload limit is 50 MB; leave a little margin.
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_MEDIA_ITEMS = 10
DOWNLOAD_TIMEOUT_SECONDS = 150

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("instagram-telegram-bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")
if WEBHOOK_SECRET and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET must contain only letters, numbers, '_' or '-'")


# A single concurrent download is friendlier to Koyeb's small free instance.
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)

bot = Application.builder().token(BOT_TOKEN).build()


# ------------------------------ URL checks -------------------------------
INSTAGRAM_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "instagr.am",
    "www.instagr.am",
}
POST_PATH = re.compile(
    r"^/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$"
    r"|^/share/(?:reel|p)/[A-Za-z0-9_-]+/?$"
    r"|^/share/[A-Za-z0-9_-]+/?$",
    re.IGNORECASE,
)


def normalize_instagram_url(raw_text: str) -> str | None:
    """Extract and validate one Instagram post/reel URL from a Telegram message."""
    match = re.search(r"https?://[^\s<>]+", raw_text.strip(), flags=re.IGNORECASE)
    if not match:
        return None

    candidate = match.group(0).rstrip(".,);]}")
    if len(candidate) > 2048:
        return None

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in INSTAGRAM_HOSTS:
        return None
    if parsed.username or parsed.password:
        return None
    if not POST_PATH.fullmatch(parsed.path):
        return None

    # Always use HTTPS after validating the host and path.
    return parsed._replace(scheme="https", fragment="").geturl()


# ------------------------------- Downloading -----------------------------
class DownloadFailure(Exception):
    pass


def find_media_files(folder: Path) -> list[Path]:
    ignored = {".part", ".ytdl", ".json", ".txt", ".description"}
    files = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() not in ignored and not p.name.startswith(".")
    ]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def run_extractor(command: list[str], folder: Path) -> list[Path]:
    try:
        result = subprocess.run(
            command,
            cwd=str(folder),
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadFailure("The download took too long. Try again later.") from exc
    except FileNotFoundError as exc:
        raise DownloadFailure("The media downloader is not installed on the server.") from exc

    files = find_media_files(folder)
    if result.returncode == 0 and files:
        return files

    details = (result.stderr or result.stdout or "").strip()
    log.warning("Downloader returned %s: %s", result.returncode, details[-1200:])
    return files


def download_instagram_media(url: str, folder: Path) -> list[Path]:
    """Try yt-dlp first, then gallery-dl for image/carousel posts."""
    # First attempt: yt-dlp handles many Reels and video posts.
    ytdlp_cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--no-playlist",
        "--socket-timeout", "20",
        "--retries", "1",
        "--max-filesize", "48M",
        "-o", str(folder / "%(autonumber)03d-%(id)s.%(ext)s"),
        url,
    ]
    try:
        files = run_extractor(ytdlp_cmd, folder)
    except DownloadFailure:
        files = []
    if files:
        return files

    # Second attempt: gallery-dl is useful for photo posts and carousels.
    gallery_dir = folder / "gallery-dl"
    gallery_dir.mkdir(parents=True, exist_ok=True)
    gallery_cmd = ["gallery-dl", "--quiet", "-D", str(gallery_dir), url]
    try:
        files = run_extractor(gallery_cmd, gallery_dir)
    except DownloadFailure:
        files = []
    if files:
        return files

    raise DownloadFailure(
        "Instagram did not provide downloadable media. The post may be private, "
        "unavailable, age-restricted, or Instagram may be blocking automated requests. "
        "This bot only supports publicly accessible posts/Reels; it does not log in to accounts."
    )


# ------------------------------- Bot handlers ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Send me a public Instagram post or Reel link.\n\n"
            "I will try to return its photos or video. Private posts and login-only content "
            "are not supported. Only download media you own or have permission to save."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "How to use:\n"
            "1. Open a public Instagram post or Reel.\n"
            "2. Copy its link.\n"
            "3. Send the link here.\n\n"
            "Supported link types: /p/, /reel/, /reels/ and /tv/.\n"
            "Maximum upload size is about 48 MB per file."
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    url = normalize_instagram_url(message.text)
    if not url:
        await message.reply_text(
            "Please send a valid Instagram post or Reel URL, such as a link containing "
            "instagram.com/p/ or instagram.com/reel/."
        )
        return

    status = await message.reply_text("⏳ Checking the link and downloading the media…")

    async with DOWNLOAD_SEMAPHORE:
        try:
            with tempfile.TemporaryDirectory(prefix="igbot-") as temp_dir:
                files = await asyncio.to_thread(
                    download_instagram_media, url, Path(temp_dir)
                )
                usable: list[Path] = []
                total_bytes = 0
                skipped = 0

                for path in files:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    if size <= 0 or size > MAX_FILE_BYTES:
                        skipped += 1
                        continue
                    if total_bytes + size > MAX_TOTAL_BYTES or len(usable) >= MAX_MEDIA_ITEMS:
                        skipped += 1
                        continue
                    usable.append(path)
                    total_bytes += size

                if not usable:
                    await status.edit_text(
                        "I found media, but it exceeded the current size limits. "
                        "Each file must be under 48 MB."
                    )
                    return

                await status.edit_text(f"✅ Found {len(usable)} file(s). Sending them now…")
                sent = 0
                for path in usable:
                    suffix = path.suffix.lower()
                    with path.open("rb") as media_file:
                        if suffix in {".jpg", ".jpeg", ".png"} and path.stat().st_size <= 10 * 1024 * 1024:
                            await context.bot.send_photo(
                                chat_id=message.chat_id,
                                photo=media_file,
                                caption="Downloaded from the Instagram link",
                            )
                        elif suffix == ".mp4":
                            await context.bot.send_video(
                                chat_id=message.chat_id,
                                video=media_file,
                                caption="Downloaded from the Instagram link",
                                supports_streaming=True,
                            )
                        else:
                            await context.bot.send_document(
                                chat_id=message.chat_id,
                                document=media_file,
                                filename=path.name,
                            )
                    sent += 1

                final_text = f"Done — sent {sent} file(s)."
                if skipped:
                    final_text += f"\n{skipped} file(s) were skipped because of size or item limits."
                await status.edit_text(final_text)

        except DownloadFailure as exc:
            await status.edit_text(str(exc))
        except Exception:
            log.exception("Unexpected error while processing a link")
            await status.edit_text(
                "Something went wrong while processing this link. Please try again later."
            )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Telegram handler error", exc_info=context.error)


bot.add_handler(CommandHandler("start", start))
bot.add_handler(CommandHandler("help", help_command))
bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
bot.add_error_handler(handle_error)


# -------------------------------- Web app ---------------------------------
app = FastAPI(title="Instagram Telegram Downloader Bot")


@app.get("/")
async def home() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "instagram-telegram-bot"})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "healthy"})


@app.on_event("startup")
async def on_startup() -> None:
    await bot.initialize()
    await bot.start()

    if PUBLIC_BASE_URL:
        if not PUBLIC_BASE_URL.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must start with https://")
        await bot.bot.set_webhook(
            url=f"{PUBLIC_BASE_URL}/telegram",
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("Telegram webhook configured at %s/telegram", PUBLIC_BASE_URL)
    else:
        log.warning("PUBLIC_BASE_URL is empty; the Telegram webhook has not been configured")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await bot.stop()
    await bot.shutdown()


@app.post("/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if WEBHOOK_SECRET and not hmac.compare_digest(
        x_telegram_bot_api_secret_token or "", WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        payload = await request.json()
        update = Update.de_json(payload, bot.bot)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc

    # Acknowledge Telegram immediately; the potentially slow download runs in the background.
    task = asyncio.create_task(bot.process_update(update))
    task.add_done_callback(_log_background_task)
    return JSONResponse({"ok": True})


def _log_background_task(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        log.exception("Background update task failed")
