import json
import logging
import os
import secrets
import tempfile
from pathlib import Path
from dotenv import load_dotenv
import qbittorrentapi
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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


SAVE_PATHS_FILE = Path(__file__).resolve().parent / "save_paths.json"


def _load_save_paths() -> list[tuple[str, str]]:
    if not SAVE_PATHS_FILE.exists():
        logging.error("Save paths file not found: %s", SAVE_PATHS_FILE)
        raise SystemExit(1)
    try:
        data = json.loads(SAVE_PATHS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logging.error("Could not read %s: %s", SAVE_PATHS_FILE, e)
        raise SystemExit(1)
    if not isinstance(data, list) or not data:
        logging.error("%s must be a non-empty JSON array", SAVE_PATHS_FILE)
        raise SystemExit(1)
    entries: list[tuple[str, str]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            logging.error("%s entry #%d is not an object", SAVE_PATHS_FILE, i)
            raise SystemExit(1)
        label, path = item.get("label"), item.get("path")
        if not isinstance(label, str) or not label.strip():
            logging.error("%s entry #%d is missing a non-empty 'label'", SAVE_PATHS_FILE, i)
            raise SystemExit(1)
        if not isinstance(path, str) or not path.strip():
            logging.error("%s entry #%d is missing a non-empty 'path'", SAVE_PATHS_FILE, i)
            raise SystemExit(1)
        entries.append((label.strip(), path.strip()))
    return entries


BOT_TOKEN        = _require_env("BOT_TOKEN")
ALLOWED_USER_IDS = _require_env_int_set("ALLOWED_USER_IDS")
QB_HOST          = _require_env("QB_HOST")
QB_PORT          = _require_env_int("QB_PORT")
QB_USER          = _require_env("QB_USER")
QB_PASS          = _require_env("QB_PASS")
SAVE_PATHS       = _load_save_paths()

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


def _picker_for(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup.from_column(
        [InlineKeyboardButton(label, callback_data=f"save|{token}|{idx}")
         for idx, (label, _) in enumerate(SAVE_PATHS)]
    )


def _is_authorized(user_id: int | None) -> bool:
    return user_id is not None and user_id in ALLOWED_USER_IDS


async def _authorize_message(update: Update) -> bool:
    if update.message is None or update.effective_user is None:
        return False
    if not _is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized")
        return False
    return True


def _pending(context: ContextTypes.DEFAULT_TYPE) -> dict:
    assert context.user_data is not None
    return context.user_data.setdefault("pending", {})


async def handle_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize_message(update):
        return
    assert update.message is not None and update.effective_user is not None
    if update.message.document is None:
        return

    user_id = update.effective_user.id
    file_name = update.message.document.file_name
    file = await update.message.document.get_file()
    tmp = tempfile.NamedTemporaryFile(suffix=".torrent", delete=False)
    tmp.close()
    await file.download_to_drive(tmp.name)

    if len(SAVE_PATHS) == 1:
        _, save_path = SAVE_PATHS[0]
        try:
            _qb_add(torrent_files=tmp.name, save_path=save_path)
            logging.info("Torrent added by user %s: %s", user_id, file_name)
            await update.message.reply_text("✅ Added to qBittorrent")
        except Exception as e:
            logging.exception("Failed to add torrent from user %s: %s", user_id, file_name)
            await update.message.reply_text(f"❌ Error: {e}")
        finally:
            os.unlink(tmp.name)
        return

    token = secrets.token_urlsafe(8)
    _pending(context)[token] = {"type": "file", "path": tmp.name, "name": file_name}
    await update.message.reply_text(
        "Where should this torrent be saved?",
        reply_markup=_picker_for(token),
    )


async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize_message(update):
        return
    assert update.message is not None and update.effective_user is not None
    if update.message.text is None:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text.startswith("magnet:"):
        await update.message.reply_text("Send a .torrent file or a magnet link")
        return

    if len(SAVE_PATHS) == 1:
        _, save_path = SAVE_PATHS[0]
        try:
            _qb_add(urls=text, save_path=save_path)
            logging.info("Magnet added by user %s: %s", user_id, text)
            await update.message.reply_text("✅ Magnet added to qBittorrent")
        except Exception as e:
            logging.exception("Failed to add magnet from user %s: %s", user_id, text)
            await update.message.reply_text(f"❌ Error: {e}")
        return

    token = secrets.token_urlsafe(8)
    _pending(context)[token] = {"type": "magnet", "url": text}
    await update.message.reply_text(
        "Where should this magnet be saved?",
        reply_markup=_picker_for(token),
    )


async def handle_save_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()

    if not _is_authorized(update.effective_user.id):
        await query.edit_message_text("Unauthorized")
        return

    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "save":
        await query.edit_message_text("⚠️ Invalid selection")
        return
    token = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await query.edit_message_text("⚠️ Invalid selection")
        return
    if not 0 <= idx < len(SAVE_PATHS):
        await query.edit_message_text("⚠️ Save path no longer exists, please re-send.")
        return

    entry = _pending(context).pop(token, None)
    if entry is None:
        await query.edit_message_text("⚠️ This upload expired, please re-send.")
        return

    user_id = update.effective_user.id
    label, save_path = SAVE_PATHS[idx]
    try:
        if entry["type"] == "file":
            _qb_add(torrent_files=entry["path"], save_path=save_path)
            logging.info("Torrent added by user %s to %s: %s", user_id, label, entry["name"])
            await query.edit_message_text(f"✅ Added to qBittorrent → {label}")
        else:
            _qb_add(urls=entry["url"], save_path=save_path)
            logging.info("Magnet added by user %s to %s: %s", user_id, label, entry["url"])
            await query.edit_message_text(f"✅ Magnet added to qBittorrent → {label}")
    except Exception as e:
        logging.exception("Failed to add upload from user %s to %s", user_id, label)
        await query.edit_message_text(f"❌ Error: {e}")
    finally:
        if entry["type"] == "file":
            try:
                os.unlink(entry["path"])
            except OSError:
                pass


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(
        filters.Document.MimeType("application/x-bittorrent") | filters.Document.FileExtension("torrent"),
        handle_torrent,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet))
    app.add_handler(CallbackQueryHandler(handle_save_choice, pattern=r"^save\|"))
    app.run_polling()


if __name__ == "__main__":
    main()
