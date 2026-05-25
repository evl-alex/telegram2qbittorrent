import logging
import os
from dotenv import load_dotenv
import tempfile
import qbittorrentapi
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logging.error("Required environment variable not set: %s", name)
        raise SystemExit(1)
    return value


def _require_env_int(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError:
        logging.error("Environment variable %s must be an integer, got: %s", name, raw)
        raise SystemExit(1)


BOT_TOKEN       = _require_env("BOT_TOKEN")
ALLOWED_USER_ID = _require_env_int("ALLOWED_USER_ID")
QB_HOST         = _require_env("QB_HOST")
QB_PORT         = _require_env_int("QB_PORT")
QB_USER         = _require_env("QB_USER")
QB_PASS         = _require_env("QB_PASS")
SAVE_PATH       = _require_env("DEFAULT_SAVE_PATH")

_qb = qbittorrentapi.Client(host=QB_HOST, port=QB_PORT, username=QB_USER, password=QB_PASS)

try:
    _qb.auth_log_in()
except qbittorrentapi.LoginFailed:
    logging.error("qBittorrent login failed: invalid credentials")
    raise SystemExit(1)
except Exception as e:
    logging.error("Could not connect to qBittorrent: %s", e)
    raise SystemExit(1)


def _qb_add(**kwargs):
    """Add torrent, re-authenticating once if the session has expired."""
    try:
        _qb.torrents_add(**kwargs)
    except qbittorrentapi.Unauthorized401Error:
        _qb.auth_log_in()
        _qb.torrents_add(**kwargs)
    except Exception as e:
        logging.error("qBittorrent error: %s", e)
        raise


def is_allowed(update: Update) -> bool:
    if update.effective_user is None:
        return False
    return update.effective_user.id == ALLOWED_USER_ID


async def handle_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.document is None:
        return
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    file = await update.message.document.get_file()
    with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        try:
            _qb_add(torrent_files=tmp.name, save_path=SAVE_PATH)
            await update.message.reply_text("✅ Added to qBittorrent.")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        finally:
            os.unlink(tmp.name)


async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.text is None:
        return
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    text = update.message.text.strip()
    if not text.startswith("magnet:"):
        await update.message.reply_text("Send a .torrent file or a magnet link.")
        return

    try:
        _qb_add(urls=text, save_path=SAVE_PATH)
        await update.message.reply_text("✅ Magnet added to qBittorrent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(
        filters.Document.MimeType("application/x-bittorrent") | filters.Document.FileExtension("torrent"),
        handle_torrent,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet))
    app.run_polling()


if __name__ == "__main__":
    main()
