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


def _require_env_int_set(name: str) -> frozenset[int]:
    raw = _require_env(name)
    try:
        ids = frozenset(int(part) for part in raw.split(",") if part.strip())
    except ValueError:
        logging.error("Environment variable %s must be a comma-separated list of integers, got: %s", name, raw)
        raise SystemExit(1)
    if not ids:
        logging.error("Environment variable %s must contain at least one integer", name)
        raise SystemExit(1)
    return ids


BOT_TOKEN        = _require_env("BOT_TOKEN")
ALLOWED_USER_IDS = _require_env_int_set("ALLOWED_USER_IDS")
QB_HOST          = _require_env("QB_HOST")
QB_PORT          = _require_env_int("QB_PORT")
QB_USER          = _require_env("QB_USER")
QB_PASS          = _require_env("QB_PASS")
SAVE_PATH        = _require_env("DEFAULT_SAVE_PATH")

_qb = qbittorrentapi.Client(host=QB_HOST, port=QB_PORT, username=QB_USER, password=QB_PASS)

try:
    _qb.auth_log_in()
except qbittorrentapi.LoginFailed:
    logging.error("qBittorrent login failed: invalid credentials")
    raise SystemExit(1)
except Exception as e:
    logging.error("Could not connect to qBittorrent: %s", e)
    raise SystemExit(1)


def _qb_add(**kwargs) -> None:
    try:
        result = _qb.torrents_add(**kwargs)
    except qbittorrentapi.Unauthorized401Error:
        _qb.auth_log_in()
        result = _qb.torrents_add(**kwargs)
    if result != "Ok.":
        raise RuntimeError(f"qBittorrent rejected the torrent: {result!r}")


async def _authorize(update: Update) -> bool:
    if update.message is None or update.effective_user is None:
        return False
    if update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Unauthorized")
        return False
    return True


async def handle_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update):
        return
    assert update.message is not None and update.effective_user is not None
    if update.message.document is None:
        return

    user_id = update.effective_user.id
    file_name = update.message.document.file_name
    file = await update.message.document.get_file()
    with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        try:
            _qb_add(torrent_files=tmp.name, save_path=SAVE_PATH)
            logging.info("Torrent added by user %s: %s", user_id, file_name)
            await update.message.reply_text("✅ Added to qBittorrent")
        except Exception as e:
            logging.exception("Failed to add torrent from user %s: %s", user_id, file_name)
            await update.message.reply_text(f"❌ Error: {e}")
        finally:
            os.unlink(tmp.name)


async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update):
        return
    assert update.message is not None and update.effective_user is not None
    if update.message.text is None:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text.startswith("magnet:"):
        await update.message.reply_text("Send a .torrent file or a magnet link")
        return

    try:
        _qb_add(urls=text, save_path=SAVE_PATH)
        logging.info("Magnet added by user %s: %s", user_id, text)
        await update.message.reply_text("✅ Magnet added to qBittorrent")
    except Exception as e:
        logging.exception("Failed to add magnet from user %s: %s", user_id, text)
        await update.message.reply_text(f"❌ Error: {e}")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(
        filters.Document.MimeType("application/x-bittorrent") | filters.Document.FileExtension("torrent"),
        handle_torrent,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet))
    app.run_polling()


if __name__ == "__main__":
    main()
