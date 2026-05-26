import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from dotenv import load_dotenv
import qbittorrentapi
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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


def _qb_torrents_info(**kwargs):
    try:
        return _qb.torrents_info(**kwargs)
    except qbittorrentapi.Unauthorized401Error:
        _qb.auth_log_in()
        return _qb.torrents_info(**kwargs)


def _snapshot_hashes() -> set[str]:
    return {t.hash for t in _qb_torrents_info()}


def _find_new_hash(before: set[str]) -> str | None:
    for _ in range(3):
        new = {t.hash for t in _qb_torrents_info()} - before
        if len(new) == 1:
            return next(iter(new))
        if len(new) > 1:
            return None
        time.sleep(0.5)
    return None


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


def _format_speed(bytes_per_sec: int) -> str:
    return f"{_format_bytes(bytes_per_sec)}/s"


def _format_duration(seconds: int) -> str:
    if seconds is None or seconds < 0 or seconds >= 8640000:
        return "∞"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _truncate_name(name: str, max_len: int = 40) -> str:
    if len(name) <= max_len:
        return name
    keep = max_len - 1
    head = keep // 2 + keep % 2
    tail = keep // 2
    return f"{name[:head]}…{name[-tail:]}"


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


def _downloading_text(name: str, label: str, progress: float, dlspeed: int, num_seeds: int, eta: int) -> str:
    return (
        f"📥 Downloading {name} → {label}\n"
        f"{int(progress * 100)}% - {_format_speed(dlspeed)} ({num_seeds} seeds) - {_format_duration(eta)}"
    )


async def _edit_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str) -> bool:
    try:
        await context.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return False
        raise


async def _status_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    assert job is not None and isinstance(job.data, dict)
    data = job.data
    torrent_hash: str = data["hash"]
    chat_id: int = data["chat_id"]
    message_id: int = data["message_id"]
    label: str = data["label"]
    name: str = data["name"]

    try:
        torrents = _qb_torrents_info(torrent_hashes=torrent_hash)
    except Exception:
        logging.exception("Failed to fetch torrent info for %s", torrent_hash)
        return

    if not torrents:
        try:
            await _edit_message(context, chat_id, message_id, f"⚠️ Torrent no longer in qBittorrent → {label}")
        except Exception:
            logging.exception("Failed to edit message for removed torrent %s", torrent_hash)
        job.schedule_removal()
        return

    t = torrents[0]
    if t.completion_on and t.completion_on > 0:
        elapsed = max(0, int(t.completion_on - t.added_on))
        text = f"✅ {name} ({_format_bytes(t.size)}) successfully downloaded to {label} in {_format_duration(elapsed)}"
        try:
            await _edit_message(context, chat_id, message_id, text)
        except Exception:
            logging.exception("Failed to edit completion message for %s", torrent_hash)
        job.schedule_removal()
        return

    text = _downloading_text(name, label, t.progress, t.dlspeed, t.num_seeds, t.eta)
    if text == data.get("last_text"):
        return
    try:
        if await _edit_message(context, chat_id, message_id, text):
            data["last_text"] = text
    except Exception:
        logging.exception("Failed to edit progress message for %s", torrent_hash)


async def _start_monitor(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    torrent_hash: str,
    label: str,
) -> None:
    if context.job_queue is None:
        logging.warning("JobQueue unavailable; status monitoring disabled")
        return

    torrents = _qb_torrents_info(torrent_hashes=torrent_hash)
    if not torrents:
        return
    raw_name = torrents[0].name or torrent_hash[:8]
    name = _truncate_name(raw_name)

    initial = _downloading_text(name, label, 0.0, 0, 0, -1)
    try:
        await _edit_message(context, chat_id, message_id, initial)
    except Exception:
        logging.exception("Failed to set initial status message for %s", torrent_hash)

    context.job_queue.run_repeating(
        _status_job,
        interval=10,
        first=10,
        data={
            "hash": torrent_hash,
            "chat_id": chat_id,
            "message_id": message_id,
            "label": label,
            "name": name,
            "last_text": initial,
        },
        name=f"mon-{torrent_hash}",
    )


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
        label, save_path = SAVE_PATHS[0]
        try:
            before = _snapshot_hashes()
            _qb_add(torrent_files=tmp.name, save_path=save_path)
            logging.info("Torrent added by user %s: %s", user_id, file_name)
            message = await update.message.reply_text(f"✅ Added to qBittorrent → {label}")
            new_hash = _find_new_hash(before)
            if new_hash:
                await _start_monitor(context, message.chat_id, message.message_id, new_hash, label)
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
        label, save_path = SAVE_PATHS[0]
        try:
            before = _snapshot_hashes()
            _qb_add(urls=text, save_path=save_path)
            logging.info("Magnet added by user %s: %s", user_id, text)
            message = await update.message.reply_text(f"✅ Magnet added to qBittorrent → {label}")
            new_hash = _find_new_hash(before)
            if new_hash:
                await _start_monitor(context, message.chat_id, message.message_id, new_hash, label)
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
        before = _snapshot_hashes()
        if entry["type"] == "file":
            _qb_add(torrent_files=entry["path"], save_path=save_path)
            logging.info("Torrent added by user %s to %s: %s", user_id, label, entry["name"])
            await query.edit_message_text(f"✅ Added to qBittorrent → {label}")
        else:
            _qb_add(urls=entry["url"], save_path=save_path)
            logging.info("Magnet added by user %s to %s: %s", user_id, label, entry["url"])
            await query.edit_message_text(f"✅ Magnet added to qBittorrent → {label}")
        new_hash = _find_new_hash(before)
        if new_hash and query.message is not None:
            await _start_monitor(context, query.message.chat.id, query.message.message_id, new_hash, label)
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
