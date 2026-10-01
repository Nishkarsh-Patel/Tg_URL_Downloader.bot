"""
URL Downloader Telegram Bot (2GB support via local Bot API server)
--------------------------------------------------------------------
User koi bhi direct file URL bhejta hai, bot use download karke Telegram
par wapas bhej deta hai — live progress (size/percent/ETA) aur Cancel
button ke saath, dono download aur upload phase mein.

2GB tak files support karta hai kyunki ye LOCAL Bot API server ke against
chalta hai (cloud API ka default 50MB limit yahan apply nahi hota).

Requirements:
    pip install python-telegram-bot requests

Setup:
    1. Pehle local Bot API server chalao (README_LOCAL_SERVER.md dekho)
    2. @BotFather se bot banao aur token lo
    3. Neeche BOT_TOKEN variable mein token daalo
    4. LOCAL_API_URL ko apne local server ke address se match karo
    5. python url_downloader_bot.py chalao

Limits:
    - Local Bot API server ke saath 2GB tak files handle hoti hain
    - Bot token hi use hota hai, koi personal account/phone login nahi chahiye
"""

import os
import math
import time
import uuid
import asyncio
import logging
import threading
import requests
from urllib.parse import urlparse, unquote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)
from telegram.request import HTTPXRequest

# ---- Config ----
# Render par: token yahan mat likho, Render ke "Environment" tab mein
# BOT_TOKEN naam se set karo. Termux/local testing ke liye, environment
# variable na mile to neeche wali fallback value use hogi.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")  # <-- Termux testing ke liye yahan daal sakte ho
LOCAL_API_URL = "http://localhost:8081"    # <-- local Bot API server ka address
DOWNLOAD_DIR = "downloads"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024     # 2 GB (local Bot API server limit)

# Render web service par ye dono automatically set ho jaate hain — kuch karna
# nahi hai. Agar ye set nahi hai (jaise Termux/local testing mein), bot khud
# purane polling mode mein chal jaayega.
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 10000))

# Kai servers bina proper User-Agent ke request reject kar dete hain —
# isliye ek common browser User-Agent sabhi requests mein bhejenge.
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Chal rahe download/upload operations, taaki Cancel button unhe rok sake.
# key: operation id (str) -> value: threading.Event
ACTIVE_OPERATIONS = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class UploadCancelled(Exception):
    """Upload ke beech mein Cancel dabane par raise hoti hai."""
    pass


class ProgressFileWrapper:
    """
    File object ko wrap karta hai taaki upload ke time read() calls track ho
    sakein (progress ke liye) aur cancel_event set hone par upload turant
    rok diya jaaye.
    """
    def __init__(self, file_obj, total_size, state, cancel_event):
        self._file = file_obj
        self._total = total_size
        self._state = state
        self._cancel_event = cancel_event
        self._read_bytes = 0

    def read(self, size=-1):
        if self._cancel_event.is_set():
            raise UploadCancelled("Upload cancel kar diya gaya")
        chunk = self._file.read(size)
        self._read_bytes += len(chunk)
        self._state["current"] = self._read_bytes
        return chunk

    def __len__(self):
        return self._total

    def __getattr__(self, name):
        return getattr(self._file, name)


def human_size(num_bytes: float) -> str:
    """Bytes ko readable GB/MB string mein badalta hai."""
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.2f}GB"
    return f"{num_bytes / (1024 ** 2):.1f}MB"


def format_eta(seconds):
    """Seconds ko 'X min Y sec left' jaisa readable text banata hai."""
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} sec left"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min left" if secs == 0 else f"{minutes} min {secs} sec left"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr {minutes} min left"


def format_speed(bytes_per_sec):
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return None
    return f"{human_size(bytes_per_sec)}/s"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Namaste! Mujhe koi bhi direct file URL bhejo, main use download "
        "karke aapko wapas bhej dunga.\n\n"
        f"⚠️ Note: File size {human_size(MAX_FILE_SIZE)} se zyada ho to main "
        "pehle bata dunga aur options dunga, seedha download try nahi karunga."
    )


def get_filename_from_url(url: str, headers: dict) -> str:
    """URL ya response headers se filename nikalta hai."""
    cd = headers.get("content-disposition")
    if cd and "filename=" in cd:
        filename = cd.split("filename=")[-1].strip('"; ')
        if filename:
            return filename

    parsed = urlparse(url)
    filename = os.path.basename(unquote(parsed.path))
    if filename:
        return filename

    return "downloaded_file"


def get_remote_file_size(url: str):
    """
    Pehle HEAD request se size pata karne ki koshish karta hai.
    Agar server HEAD support na kare ya Content-Length na de, to
    GET request stream=True se karke sirf headers padhta hai
    (body abhi tak download nahi hua hota).
    Returns: (size_in_bytes_or_None, headers, response_to_reuse_or_None)
    """
    try:
        head_resp = requests.head(url, allow_redirects=True, headers=COMMON_HEADERS, timeout=15)
        content_length = head_resp.headers.get("content-length")
        if content_length:
            return int(content_length), head_resp.headers, None
    except requests.exceptions.RequestException:
        pass

    # HEAD se nahi mila, ab GET stream se headers check karo
    get_resp = requests.get(url, stream=True, headers=COMMON_HEADERS, timeout=30)
    get_resp.raise_for_status()
    content_length = get_resp.headers.get("content-length")
    size = int(content_length) if content_length else None
    # get_resp wapas bhejte hain taaki same connection reuse ho sake (dobara GET na karna pade)
    return size, get_resp.headers, get_resp


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Ye valid URL nahi lag raha. Http(s):// se start hona chahiye.")
        return

    status_msg = await update.message.reply_text("🔍 File size check ho raha hai...")

    try:
        size, headers, reusable_response = get_remote_file_size(url)
        filename = get_filename_from_url(url, headers)

        # URL/filename/response ko user_data mein save karo (callback data mein size limit hoti hai)
        context.user_data["pending_download"] = {
            "url": url,
            "filename": filename,
            "total_size": size,
        }
        context.user_data["_reusable_response"] = reusable_response

        if size is not None and size > MAX_FILE_SIZE:
            # 2GB se zyada — Cancel ya Split ke options
            num_parts = math.ceil(size / MAX_FILE_SIZE)
            part_sizes = []
            remaining = size
            for _ in range(num_parts):
                part = min(MAX_FILE_SIZE, remaining)
                part_sizes.append(part)
                remaining -= part
            parts_desc = " + ".join(human_size(p) for p in part_sizes)

            if reusable_response is not None:
                reusable_response.close()
                context.user_data["_reusable_response"] = None

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="dl_cancel")],
                [InlineKeyboardButton(f"✂️ Split karke bhejo ({parts_desc})", callback_data="dl_split")],
            ])

            await status_msg.edit_text(
                f"⚠️ File size *{human_size(size)}* hai, jo limit "
                f"({human_size(MAX_FILE_SIZE)}) se zyada hai.\n\n"
                "Ek pura file bot nahi bhej payega. Kya karna hai?",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

        # 2GB se kam (ya size pata nahi chala) — phir bhi Cancel/Download options dikhao
        size_text = human_size(size) if size is not None else "pata nahi chal paya"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="dl_cancel")],
            [InlineKeyboardButton("✅ Download", callback_data="dl_download")],
        ])

        await status_msg.edit_text(
            f"📦 File: *{filename}*\n"
            f"📏 Size: *{size_text}*\n\n"
            "Kya karna hai?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    except requests.exceptions.RequestException as e:
        logger.error(f"Size check error: {e}")
        await status_msg.edit_text(f"❌ URL check karte waqt error aaya: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await status_msg.edit_text("❌ Kuch galat ho gaya. Dobara try karo.")


async def run_progress_updates(status_msg, label, state, op_id):
    """
    Background task: har 2.5 second mein status_msg ko progress ke sath
    update karta hai, jab tak state['done'] True na ho jaaye.
    """
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_op:{op_id}")]])
    start_time = time.monotonic()
    last_text = None

    while not state.get("done"):
        current = state.get("current", 0)
        total = state.get("total")
        elapsed = time.monotonic() - start_time
        rate = current / elapsed if elapsed > 1 else 0

        if total:
            percent = min(100, int(current / total * 100))
            speed = format_speed(rate)
            speed_part = f" | {speed}" if speed else ""
            eta = format_eta((total - current) / rate) if rate > 0 else None
            eta_part = f" | {eta}" if eta else ""
            text = f"{label}\n{human_size(current)} / {human_size(total)}{speed_part}{eta_part} | {percent}%"
        else:
            speed = format_speed(rate)
            speed_part = f" | {speed}" if speed else ""
            text = f"{label}\n{human_size(current)} / ?{speed_part}"

        if text != last_text:
            try:
                await status_msg.edit_text(text, reply_markup=keyboard)
            except Exception:
                pass  # "message not modified" jaisi harmless errors ignore karo
            last_text = text

        await asyncio.sleep(2.5)


def _blocking_download(response, filepath, max_size, cancel_event, state):
    """
    Ek alag thread mein chalta hai (event loop block na ho isliye).
    Response ko file mein likhta hai, state['current'] update karta rehta hai,
    aur cancel_event set hone par turant ruk jaata hai.
    Returns: "ok" / "cancelled" / "too_large"
    """
    downloaded = 0
    try:
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                if cancel_event.is_set():
                    return "cancelled"
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_size:
                    return "too_large"
                f.write(chunk)
                state["current"] = downloaded
        return "ok"
    finally:
        state["done"] = True


async def download_and_send(message, status_msg, context, url, filename, existing_response=None):
    """
    File ko download karke Telegram par bhejta hai — thread mein (taaki
    event loop free rahe aur Cancel/naye commands turant kaam karein),
    progress aur Cancel ke saath.
    """
    op_id = uuid.uuid4().hex[:8]
    cancel_event = threading.Event()
    ACTIVE_OPERATIONS[op_id] = cancel_event
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    try:
        response = existing_response if existing_response is not None else requests.get(
            url, stream=True, headers=COMMON_HEADERS, timeout=30
        )
        response.raise_for_status()

        content_length = response.headers.get("content-length")
        known_total = int(content_length) if content_length else None

        dl_state = {"current": 0, "total": known_total, "done": False}
        progress_task = asyncio.create_task(
            run_progress_updates(status_msg, "⬇️ File download ho rahi hai", dl_state, op_id)
        )

        result = await asyncio.to_thread(_blocking_download, response, filepath, MAX_FILE_SIZE, cancel_event, dl_state)

        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        if result == "cancelled":
            await status_msg.edit_text("❌ Download cancel kar diya gaya.")
            return
        if result == "too_large":
            await status_msg.edit_text(
                f"❌ File limit ({human_size(MAX_FILE_SIZE)}) se badi nikli "
                "(server ne sahi size nahi bataya tha), download rok diya gaya."
            )
            return

        cancel_event.clear()
        file_size_on_disk = os.path.getsize(filepath)
        up_state = {"current": 0, "total": file_size_on_disk, "done": False}
        upload_progress_task = asyncio.create_task(
            run_progress_updates(status_msg, "⬆️ File upload ho rahi hai", up_state, op_id)
        )

        try:
            with open(filepath, "rb") as f:
                wrapped = ProgressFileWrapper(f, file_size_on_disk, up_state, cancel_event)
                await message.reply_document(document=wrapped, filename=filename)
        except UploadCancelled:
            up_state["done"] = True
            upload_progress_task.cancel()
            try:
                await upload_progress_task
            except asyncio.CancelledError:
                pass
            await status_msg.edit_text("❌ Upload cancel kar diya gaya.")
            return

        up_state["done"] = True
        upload_progress_task.cancel()
        try:
            await upload_progress_task
        except asyncio.CancelledError:
            pass

        await status_msg.delete()

    except requests.exceptions.RequestException as e:
        logger.error(f"Download error: {e}")
        await status_msg.edit_text(f"❌ Download fail ho gaya: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await status_msg.edit_text("❌ Kuch galat ho gaya. Dobara try karo.")
    finally:
        ACTIVE_OPERATIONS.pop(op_id, None)
        if os.path.exists(filepath):
            os.remove(filepath)


def _blocking_split_download(response, base_filepath, max_part_size, cancel_event, state):
    """
    Ek alag thread mein chalta hai. File ko max_part_size ke parts mein
    split karke disk par likhta hai, cumulative progress state update
    karta hai, cancel_event set hone par turant ruk jaata hai.
    Returns: (result, part_paths) jahan result "ok"/"cancelled"
    """
    part_paths = []
    downloaded_total = 0
    part_num = 1
    current_size = 0
    current_path = f"{base_filepath}.part{part_num}"
    part_paths.append(current_path)
    current_file = open(current_path, "wb")

    try:
        for chunk in response.iter_content(chunk_size=65536):
            if cancel_event.is_set():
                return "cancelled", part_paths
            if not chunk:
                continue

            if current_size + len(chunk) > max_part_size:
                space_left = max_part_size - current_size
                current_file.write(chunk[:space_left])
                current_file.close()
                downloaded_total += space_left

                part_num += 1
                current_path = f"{base_filepath}.part{part_num}"
                part_paths.append(current_path)
                current_file = open(current_path, "wb")
                remainder = chunk[space_left:]
                current_file.write(remainder)
                current_size = len(remainder)
                downloaded_total += len(remainder)
            else:
                current_file.write(chunk)
                current_size += len(chunk)
                downloaded_total += len(chunk)

            state["current"] = downloaded_total

        return "ok", part_paths
    finally:
        state["done"] = True
        if not current_file.closed:
            current_file.close()


async def download_and_send_split(message, status_msg, context, url, filename):
    """File ko download karke MAX_FILE_SIZE ke parts mein split karta hai aur har part progress+Cancel ke saath bhejta hai (disk-based, thread mein)."""
    base_filepath = os.path.join(DOWNLOAD_DIR, filename)
    part_paths = []
    op_id = uuid.uuid4().hex[:8]
    cancel_event = threading.Event()
    ACTIVE_OPERATIONS[op_id] = cancel_event

    try:
        response = requests.get(url, stream=True, headers=COMMON_HEADERS, timeout=30)
        response.raise_for_status()

        content_length = response.headers.get("content-length")
        known_total = int(content_length) if content_length else None

        dl_state = {"current": 0, "total": known_total, "done": False}
        progress_task = asyncio.create_task(
            run_progress_updates(status_msg, "⬇️ File download ho rahi hai (split mode)", dl_state, op_id)
        )

        result, part_paths = await asyncio.to_thread(
            _blocking_split_download, response, base_filepath, MAX_FILE_SIZE, cancel_event, dl_state
        )

        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        if result == "cancelled":
            await status_msg.edit_text("❌ Download cancel kar diya gaya.")
            return

        total_parts = len(part_paths)
        for i, part_path in enumerate(part_paths, start=1):
            cancel_event.clear()
            part_filename = f"{filename}.part{i}"
            part_size = os.path.getsize(part_path)
            up_state = {"current": 0, "total": part_size, "done": False}
            upload_progress_task = asyncio.create_task(
                run_progress_updates(status_msg, f"⬆️ Part {i}/{total_parts} upload ho raha hai", up_state, op_id)
            )

            try:
                with open(part_path, "rb") as f:
                    wrapped = ProgressFileWrapper(f, part_size, up_state, cancel_event)
                    await message.reply_document(document=wrapped, filename=part_filename)
            except UploadCancelled:
                up_state["done"] = True
                upload_progress_task.cancel()
                try:
                    await upload_progress_task
                except asyncio.CancelledError:
                    pass
                await status_msg.edit_text("❌ Upload cancel kar diya gaya.")
                return

            up_state["done"] = True
            upload_progress_task.cancel()
            try:
                await upload_progress_task
            except asyncio.CancelledError:
                pass

        await status_msg.edit_text(
            f"✅ Done! File {total_parts} parts mein bhej di gayi hai.\n"
            "Sabhi parts ko combine karne ke liye (Linux/Mac):\n"
            f"`cat {filename}.part* > {filename}`",
            parse_mode="Markdown",
        )

    except requests.exceptions.RequestException as e:
        logger.error(f"Split download error: {e}")
        await status_msg.edit_text(f"❌ Download fail ho gaya: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected split error: {e}")
        await status_msg.edit_text("❌ Kuch galat ho gaya. Dobara try karo.")
    finally:
        ACTIVE_OPERATIONS.pop(op_id, None)
        for p in part_paths:
            if os.path.exists(p):
                os.remove(p)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Mid-download/upload Cancel button (progress message ke saath aata hai)
    if query.data.startswith("cancel_op:"):
        op_id = query.data.split(":", 1)[1]
        event = ACTIVE_OPERATIONS.get(op_id)
        if event:
            event.set()
        try:
            await query.answer("Cancel ho raha hai..." if event else "Ye operation ab active nahi hai.")
        except Exception:
            pass  # query "stale/expired" ho sakti hai — event already set ho chuka, aage badho
        return

    try:
        await query.answer()
    except Exception:
        pass  # query "stale/expired" ho sakti hai (slow host par delay ke wajah se) — ignore karke aage badho

    pending = context.user_data.get("pending_download")
    if not pending:
        try:
            await query.edit_message_text("⚠️ Ye request expire ho chuki hai. Naya URL bhejo.")
        except Exception:
            pass
        return

    url = pending["url"]
    filename = pending["filename"]
    reusable_response = context.user_data.pop("_reusable_response", None)

    if query.data == "dl_cancel":
        context.user_data.pop("pending_download", None)
        if reusable_response is not None:
            reusable_response.close()
        await query.edit_message_text("❌ Cancel kar diya gaya.")
        return

    if query.data == "dl_download":
        context.user_data.pop("pending_download", None)
        await download_and_send(query.message, query.message, context, url, filename, reusable_response)
        return

    if query.data == "dl_split":
        context.user_data.pop("pending_download", None)
        if reusable_response is not None:
            reusable_response.close()
        await download_and_send_split(query.message, query.message, context, url, filename)
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} ke process karte waqt exception aayi:", exc_info=context.error)


def main():
    # Default timeout (~5s) slow/unstable networks (jaise rural/mobile data) ke
    # liye kaafi kam hai — isko badha diya taaki connection retry karne ka time mile
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .concurrent_updates(True)  # taaki Cancel button download/upload ke beech mein bhi kaam kare
        # --- Local Bot API server wali lines (VPS deploy ke waqt uncomment karo) ---
        # .base_url(f"{LOCAL_API_URL}/bot")
        # .base_file_url(f"{LOCAL_API_URL}/file/bot")
        # .local_mode(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    if RENDER_EXTERNAL_URL:
        # Render par — webhook mode
        logger.info("Webhook mode mein start ho raha hai (Render)...")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}",
        )
    else:
        # Local/Termux testing — polling mode
        logger.info("Bot polling mode mein start ho raha hai...")
        app.run_polling()


if __name__ == "__main__":
    # Python 3.14+ compatibility shim:
    # Python 3.14 ne asyncio ka "implicit event loop creation" hata diya hai.
    # python-telegram-bot library ka internal code abhi bhi purane behavior
    # par depend karta hai (asyncio.get_event_loop() ek loop expect karta hai),
    # isliye yahan manually ek loop bana kar set kar rahe hain taaki library
    # ka internal call fail na ho.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    main()
