

from __future__ import annotations

import base64
import html
import json
import logging
import os
import random
import re
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("streamly")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()
META_AI_KEY = os.getenv("META_AI_API_KEY", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
META_AI_GEM_ID = "ba0fbe0d-976e-493a-afdb-6d8469e53df0"
META_AI_ENDPOINT = f"https://nxtai.zipohostbd.workers.dev/api/use?gem={META_AI_GEM_ID}"
IMAGE_FALLBACK_ENDPOINT = "https://image.pollinations.ai/prompt/"
TIKWM_ENDPOINT = "https://www.tikwm.com/api/"
SNAPTIK_ENDPOINT = "https://snaptik.app/abc2.php"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_MEDIA_BYTES = max(8, int(os.getenv("MAX_MEDIA_MB", "48"))) * 1024 * 1024
MAX_VIDEO_SECONDS = max(60, int(os.getenv("MAX_VIDEO_MINUTES", "30"))) * 60
STATE_TTL_SECONDS = 20 * 60
MAX_TEXT_LENGTH = 3900


@dataclass(frozen=True)
class Platform:
    key: str
    title: str
    emoji: str
    domains: tuple[str, ...]



TIKTOK = Platform(
    "tiktok",
    "TikTok",
    "🎵",
    ("tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
)
FACEBOOK = Platform(
    "facebook",
    "Facebook",
    "🔵",
    ("facebook.com", "fb.watch", "m.facebook.com", "www.facebook.com"),
)
INSTAGRAM = Platform(
    "instagram",
    "Instagram",
    "📸",
    ("instagram.com", "www.instagram.com"),
)
YOUTUBE = Platform(
    "youtube",
    "YouTube",
    "▶️",
    ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"),
)
PLATFORM_BY_KEY = {
    p.key: p for p in (TIKTOK, FACEBOOK, INSTAGRAM, YOUTUBE)
}

CHAT_MODES: dict[int, str] = {}
CHAT_PLATFORMS: dict[int, str] = {}
DOWNLOAD_OPTIONS: dict[int, dict[str, Any]] = {}
ACTIVE_CHATS: set[int] = set()
STATE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="streamly")
SHUTDOWN = threading.Event()


def log(message: str, *args: Any) -> None:
    LOG.info(message, *args)


def format_bytes(value: int | float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def normalize_url(value: str) -> str | None:
    candidate = value.strip()
    if not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if len(candidate) > 2048:
        return None
    return candidate


def platform_from_url(url: str) -> Platform | None:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for platform in PLATFORM_BY_KEY.values():
        if any(host == domain or host.endswith(f".{domain}") for domain in platform.domains):
            return platform
    return None


def http_request(
    url: str,
    *,
    method: str = "GET",
    payload: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 35,
    max_bytes: int = 3 * 1024 * 1024,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            total = 0
            while total < max_bytes:
                chunk = response.read(min(64 * 1024, max_bytes - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return response.status, dict(response.headers.items()), b"".join(chunks)
    except HTTPError as error:
        body = error.read(1024)
        raise RuntimeError(
            f"Remote service returned HTTP {error.code}: "
            f"{body.decode(errors='replace')[:180]}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Remote service could not be reached: {error}") from error


def json_request(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 35,
) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    request_headers = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        request_headers.update(headers)
    _, _, raw = http_request(
        url,
        method=method,
        payload=body,
        headers=request_headers,
        timeout=timeout,
        max_bytes=4 * 1024 * 1024,
    )
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Remote service returned invalid JSON") from error


def telegram_call(method: str, data: dict[str, Any] | None = None) -> Any:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TOKEN is not configured")
    payload = (data or {}).copy()
    status, _, raw = http_request(
        f"{TELEGRAM_API}/{method}",
        method="POST",
        payload=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=45,
        max_bytes=4 * 1024 * 1024,
    )
    if status >= 400:
        raise RuntimeError(f"Telegram API HTTP {status}")
    try:
        result = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Telegram returned invalid JSON") from error
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API request failed"))
    return result.get("result")


def telegram_upload(
    method: str,
    field_name: str,
    file_name: str,
    file_bytes: bytes,
    fields: dict[str, str],
) -> Any:
    boundary = f"----Streamly{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{file_name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    _, _, raw = http_request(
        f"{TELEGRAM_API}/{method}",
        method="POST",
        payload=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=120,
        max_bytes=4 * 1024 * 1024,
    )
    result = json.loads(raw.decode("utf-8", errors="replace"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram upload failed"))
    return result.get("result")


def send_message(chat_id: int | str, text: str, **extra: Any) -> Any:
    return telegram_call(
        "sendMessage",
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML", **extra},
    )


def edit_message(chat_id: int | str, message_id: int, text: str, **extra: Any) -> Any:
    try:
        return telegram_call(
            "editMessageText",
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "text": text,
                "parse_mode": "HTML",
                **extra,
            },
        )
    except RuntimeError as error:
        if "message is not modified" not in str(error).lower():
            log("Could not edit status message: %s", error)
        return None


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except RuntimeError as error:
        log("Callback acknowledgement failed: %s", error)


def inline_keyboard(rows: list[list[dict[str, str]]]) -> str:
    return json.dumps({"inline_keyboard": rows})


def main_keyboard() -> str:
    return inline_keyboard([
        [
            {"text": "⬇️ Video Downloader", "callback_data": "mode:download"},
            {"text": "🤖 AI assistant", "callback_data": "mode:ai"},
        ],
        [
            {"text": "ℹ️ How it works", "callback_data": "help"},
            {"text": "✖️ Cancel", "callback_data": "cancel"},
        ],
    ])


def platform_keyboard() -> str:
    return inline_keyboard([
        [
            {"text": "🎵 TikTok", "callback_data": "platform:tiktok"},
            {"text": "🔵 Facebook", "callback_data": "platform:facebook"},
        ],
        [
            {"text": "📸 Instagram", "callback_data": "platform:instagram"},
            {"text": "▶️ YouTube", "callback_data": "platform:youtube"},
        ],
        [{"text": "↩️ Back to menu", "callback_data": "home"}],
        [{"text": "✖️ Cancel", "callback_data": "cancel"}],
    ])


def quality_keyboard(options: list[dict[str, Any]]) -> str:
    rows: list[list[dict[str, str]]] = []
    for index, option in enumerate(options[:8]):
        size = option.get("size")
        suffix = f" · {format_bytes(size)}" if size else ""
        rows.append([{
            "text": f"🎚️ {option['label']}{suffix}",
            "callback_data": f"quality:{index}",
        }])
    rows.extend([
        [{"text": "🔁 Download another", "callback_data": "mode:download"}],
        [{"text": "✖️ Cancel", "callback_data": "cancel"}],
    ])
    return inline_keyboard(rows)



def progress_text(title: str, downloaded: int, total: int, stage: str) -> str:
    if total:
        percent = min(100, downloaded * 100 / total)
        filled = min(20, int(percent / 5))
        progress = f"{percent:5.1f}%"
        status = f"{format_bytes(downloaded)} of {format_bytes(total)}"
    else:
        filled = min(20, int(time.monotonic() * 3) % 21)
        progress = "working"
        status = f"{format_bytes(downloaded)} downloaded"
    bar = "█" * filled + "░" * (20 - filled)
    return (
        f"<b>📥 {escape(title[:70])}</b>\n\n"
        f"<code>{bar}</code>\n\n"
        f"🚀 <b>Progress:</b> {progress}\n"
        f"📶 <b>Status:</b> {escape(status)}\n"
        f"🛠 <b>Stage:</b> {escape(stage)}"
    )


def cleanup_state() -> None:
    cutoff = time.time() - STATE_TTL_SECONDS
    with STATE_LOCK:
        expired = [
            chat_id
            for chat_id, item in DOWNLOAD_OPTIONS.items()
            if float(item.get("created_at", 0)) < cutoff
        ]
        for chat_id in expired:
            DOWNLOAD_OPTIONS.pop(chat_id, None)
            CHAT_PLATFORMS.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Multi-platform downloader
# ---------------------------------------------------------------------------

def _import_yt_dlp():
    try:
        import yt_dlp  # type: ignore
        return yt_dlp
    except ImportError as error:
        raise RuntimeError(
            "yt-dlp is not installed. Add yt-dlp to requirements.txt and redeploy."
        ) from error


def _human_height(height: Any) -> int:
    try:
        return int(height or 0)
    except (TypeError, ValueError):
        return 0


def _option_label(height: int, ext: str, filesize: int) -> str:
    if height >= 2160:
        quality = "4K"
    elif height >= 1440:
        quality = "1440p"
    elif height >= 1080:
        quality = "1080p"
    elif height >= 720:
        quality = "720p"
    elif height >= 480:
        quality = "480p"
    elif height >= 360:
        quality = "360p"
    elif height:
        quality = f"{height}p"
    else:
        quality = "Best"
    suffix = f" · {format_bytes(filesize)}" if filesize else ""
    return f"{quality} {ext.upper()}{suffix}"



def _tiktok_options(title: str, video_urls: list[str]) -> tuple[str, list[dict[str, Any]]]:
    if not video_urls:
        raise RuntimeError("TikTok API did not return a downloadable video URL")
    options: list[dict[str, Any]] = []
    labels = ["HD quality", "Standard quality", "Auto quality"]
    for index, video_url in enumerate(video_urls[:3]):
        options.append({
            "format": video_url,
            "url": video_url,
            "height": 0,
            "size": 0,
            "ext": "mp4",
            "label": labels[min(index, len(labels) - 1)],
        })
    return title, options


def _resolve_tikwm(source_url: str) -> tuple[str, list[dict[str, Any]]]:
    endpoint = f"{TIKWM_ENDPOINT}?{urlencode({'url': source_url, 'hd': '1'})}"
    response = json_request(endpoint, timeout=45)
    if not isinstance(response, dict) or response.get("code") not in {0, "0", None}:
        message = response.get("msg") if isinstance(response, dict) else ""
        raise RuntimeError(str(message or "TikWM did not resolve this TikTok URL"))
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("TikWM returned no media data")
    duration = int(data.get("duration") or 0)
    if duration > MAX_VIDEO_SECONDS:
        raise RuntimeError(f"This video is {duration // 60} minutes long. The limit is {MAX_VIDEO_SECONDS // 60} minutes.")
    title = str(data.get("title") or "TikTok video")
    preferred_video = [_remote_url(data.get(k)) for k in ("hdplay", "play", "wmplay") if _remote_url(data.get(k))]
    video_urls, _ = _media_links(data)
    video_urls = list(dict.fromkeys(preferred_video + video_urls))
    title, options = _tiktok_options(title, video_urls)
    known_sizes = {data.get("hdplay"): data.get("hd_size"), data.get("play"): data.get("size"), data.get("wmplay"): data.get("wm_size")}
    for option in options:
        try:
            option["size"] = int(known_sizes.get(option["url"]) or 0)
        except (TypeError, ValueError):
            option["size"] = 0
    return title, options


def _resolve_snaptik(source_url: str) -> tuple[str, list[dict[str, Any]]]:
    body = urlencode({"url": source_url}).encode()
    _, _, raw = http_request(
        SNAPTIK_ENDPOINT, method="POST", payload=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Referer": "https://snaptik.app/"},
        timeout=45, max_bytes=4 * 1024 * 1024,
    )
    try:
        response = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError("SnapTik returned invalid JSON") from error
    if isinstance(response, dict) and response.get("success") is False:
        raise RuntimeError("SnapTik could not resolve this TikTok URL")
    video_urls, _ = _media_links(response)
    title = "TikTok video"
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            title = str(response.get("title") or data.get("title") or title)
        else:
            title = str(response.get("title") or title)
    return _tiktok_options(title, video_urls)


def tiktok_info(source_url: str) -> tuple[str, list[dict[str, Any]]]:
    errors: list[str] = []
    for resolver in (_resolve_tikwm, _resolve_snaptik):
        try:
            return resolver(source_url)
        except Exception as error:
            errors.append(str(error))
    raise RuntimeError("TikTok download APIs could not resolve this URL")

def _yt_dlp_options(source_url: str, format_selector: str | None = None) -> dict[str, Any]:
    # Video-only product: never request or convert to an audio-only output.
    # yt-dlp merges the best video + audio streams, then recodes only when
    # necessary so the final file is always an MP4 video.
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {"User-Agent": USER_AGENT},
        "js_runtimes": {"deno": {}},
        "remote_components": {"ejs": "github"},
        "format": format_selector or "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "recodevideo": "mp4",
    }

def _format_heights(info: dict[str, Any]) -> list[int]:
    heights: set[int] = set()
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        h = _human_height(fmt.get("height"))
        if 144 <= h <= 2160:
            heights.add(h)
    # Present useful standard choices, mapped to the nearest available source.
    wanted = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    return [h for h in wanted if any(x >= h for x in heights)]


def _resolve_with_ytdlp(source_url: str, platform: Platform) -> tuple[str, list[dict[str, Any]]]:
    yt_dlp = _import_yt_dlp()
    opts = _yt_dlp_options(source_url)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
    except Exception as error:
        message = str(error)
        lower = message.lower()
        if any(word in lower for word in ("private", "login", "sign in", "authentication", "members-only")):
            raise RuntimeError(
                "এই media public automated access-এর জন্য available নয়। "
                "Private/login-only content bypass করা যাবে না।"
            ) from error
        raise RuntimeError(f"{platform.title} resolve করা যায়নি: {message[:450]}") from error

    if not isinstance(info, dict):
        raise RuntimeError("Downloader returned invalid media information.")
    duration = int(info.get("duration") or 0)
    if duration > MAX_VIDEO_SECONDS:
        raise RuntimeError(
            f"এই ভিডিওটি {duration // 60} মিনিটের বেশি। সর্বোচ্চ সীমা {MAX_VIDEO_SECONDS // 60} মিনিট।"
        )
    title = str(info.get("title") or f"{platform.title} video")
    heights = _format_heights(info)
    if not heights:
        # Some extractors expose only a direct selected URL. Keep one safe fallback.
        direct = info.get("url")
        if isinstance(direct, str) and direct.startswith(("http://", "https://")):
            return title, [{
                "url": direct, "format": "best", "height": _human_height(info.get("height")),
                "size": int(info.get("filesize") or 0), "ext": "mp4",
                "label": "Auto quality", "title": title,
                "selector": "best",
            }]
        raise RuntimeError("এই media-এর জন্য downloadable video format পাওয়া যায়নি।")

    options: list[dict[str, Any]] = []
    for h in heights[:8]:
        options.append({
            "format": f"height<={h}",
            "selector": f"bestvideo*[height<={h}]+bestaudio/best[height<={h}]/best",
            "url": "",
            "height": h,
            "size": 0,
            "ext": "mp4",
            "label": f"{_option_label(h, 'mp4', 0)}",
            "title": title,
        })
    return title, options

def resolve_media(source_url: str, platform: Platform) -> tuple[str, list[dict[str, Any]]]:
    # TikTok keeps the proven direct-API path. Other platforms use yt-dlp.
    if platform.key == "tiktok":
        return tiktok_info(source_url)
    return _resolve_with_ytdlp(source_url, platform)


def _download_bytes(url: str, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=120) as response:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(
                        f"Downloaded file is larger than {format_bytes(max_bytes)}."
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPError as error:
        raise RuntimeError(f"Media server returned HTTP {error.code}.") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Media could not be downloaded: {error}") from error


def _upload_local_media(chat_id: int, title: str, path: str, caption: str) -> None:
    size = os.path.getsize(path)
    if size > MAX_MEDIA_BYTES:
        raise RuntimeError(
            f"ফাইলটি {format_bytes(size)}; Telegram limit-এর মধ্যে নেই। কম quality বেছে নিন।"
        )
    with open(path, "rb") as fh:
        data = fh.read()
    telegram_upload(
        "sendVideo", "video", safe_filename(title, "mp4"), data,
        {"chat_id": str(chat_id), "caption": caption, "supports_streaming": "true"},
    )

def _download_ytdlp_file(source_url: str, title: str, option: dict[str, Any], progress_callback: Callable[[int, int, str], None]) -> str:
    import tempfile
    yt_dlp = _import_yt_dlp()
    tmp = tempfile.mkdtemp(prefix="streamly-")
    outtmpl = os.path.join(tmp, "%(id)s.%(ext)s")
    selector = str(option.get("selector") or "bestvideo*+bestaudio/best")
    opts = _yt_dlp_options(source_url, selector)
    opts.update({"outtmpl": outtmpl})

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            progress_callback(
                int(d.get("downloaded_bytes") or 0),
                int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0),
                "Video download হচ্ছে…",
            )
        elif d.get("status") == "finished":
            progress_callback(
                int(d.get("downloaded_bytes") or 0),
                int(d.get("total_bytes") or 0),
                "Video MP4 করা হচ্ছে…",
            )
    opts["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([source_url])
        files = [
            os.path.join(tmp, name) for name in os.listdir(tmp)
            if os.path.isfile(os.path.join(tmp, name))
        ]
        mp4 = [p for p in files if p.lower().endswith(".mp4")]
        if not mp4:
            raise RuntimeError("Downloader কোনো MP4 video তৈরি করেনি।")
        return max(mp4, key=os.path.getsize)
    except Exception:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise

def send_download(chat_id: int, status_id: int, source_url: str, option: dict[str, Any]) -> None:
    last_update = 0.0
    def update(downloaded: int, total: int, stage: str) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update < 1.0 and downloaded != 0:
            return
        last_update = now
        edit_message(chat_id, status_id, progress_text(str(option.get("title", "Video")), downloaded, total, stage))

    try:
        platform_key = str(option.get("platform") or "")
        title = str(option.get("title") or "download")
        caption = f"<b>{escape(title[:900])}</b>\n\n🎬 MP4 video"

        if platform_key == "tiktok":
            media_url = _remote_url(option.get("url"))
            if not media_url:
                raise RuntimeError("TikTok selected video-এর media URL পাওয়া যায়নি।")
            update(0, int(option.get("size") or 0), "Telegram-এ video পাঠানো হচ্ছে…")
            try:
                telegram_call("sendVideo", {"chat_id": str(chat_id), "video": media_url, "caption": caption, "supports_streaming": "true"})
            except Exception:
                data = _download_bytes(media_url)
                if len(data) > MAX_MEDIA_BYTES:
                    raise RuntimeError("TikTok video Telegram limit-এর চেয়ে বড়।")
                update(len(data), len(data), "Telegram-এ video upload করা হচ্ছে…")
                telegram_upload("sendVideo", "video", safe_filename(title, "mp4"), data, {"chat_id": str(chat_id), "caption": caption, "supports_streaming": "true"})
        else:
            update(0, 0, "Video download শুরু হচ্ছে…")
            path = _download_ytdlp_file(source_url, title, option, update)
            try:
                _upload_local_media(chat_id, title, path, caption)
                update(os.path.getsize(path), os.path.getsize(path), "Telegram-এ video upload complete")
            finally:
                import shutil
                shutil.rmtree(os.path.dirname(path), ignore_errors=True)

        edit_message(chat_id, status_id, "<b>✅ Video download complete</b>\n\nআপনার MP4 video পাঠানো হয়েছে।", reply_markup=main_keyboard())
    except Exception as error:
        log("Download failed for chat %s: %s", chat_id, error)
        detail = str(error)[:700]
        lower = detail.lower()
        if "private" in lower or "sign in" in lower or "login" in lower or "members-only" in lower:
            detail = "এই media public automated access-এর জন্য available নয়। Private/login-only content bypass করা যাবে না।"
        elif "telegram limit" in lower or "larger than" in lower:
            detail = "ভিডিওটি Telegram-এর 50 MB upload limit-এর চেয়ে বড়। কম quality বেছে আবার চেষ্টা করুন।"
        edit_message(chat_id, status_id, f"<b>❌ Video download করা যায়নি</b>\n\n{escape(detail)}", reply_markup=platform_keyboard())
    finally:
        with STATE_LOCK:
            ACTIVE_CHATS.discard(chat_id)
            DOWNLOAD_OPTIONS.pop(chat_id, None)

def resolve_download(chat_id: int, status_id: int, source_url: str, platform: Platform) -> None:
    try:
        edit_message(
            chat_id,
            status_id,
            f"<b>{platform.emoji} {escape(platform.title)}</b>\n\n"
            "১/৩  Public media যাচাই করছি…",
        )
        title, options = resolve_media(source_url, platform)
        for option in options:
            option["title"] = title
            option["platform_title"] = platform.title
            option["platform"] = platform.key

        with STATE_LOCK:
            DOWNLOAD_OPTIONS[chat_id] = {
                "title": title,
                "options": options,
                "source_url": source_url,
                "platform": platform.key,
                "created_at": time.time(),
            }

        edit_message(
            chat_id,
            status_id,
            f"<b>{escape(title[:100])}</b>\n\n"
            f"২/৩  {len(options)}টি quality পাওয়া গেছে।\n"
            "একটি quality বেছে নিন:",
            reply_markup=quality_keyboard(options),
        )
    except Exception as error:
        log("Resolve failed for chat %s: %s", chat_id, error)
        edit_message(
            chat_id,
            status_id,
            f"<b>❌ {escape(platform.title)} resolve করা যায়নি</b>\n\n"
            f"{escape(str(error)[:700])}",
            reply_markup=platform_keyboard(),
        )


# ---------------------------------------------------------------------------
# AI assistant
# ---------------------------------------------------------------------------

def ai_request(message: str) -> Any:
    if not META_AI_KEY:
        raise RuntimeError("META_AI_API_KEY is not configured")
    return json_request(
        META_AI_ENDPOINT,
        method="POST",
        data={"api_key": META_AI_KEY, "message": message},
        headers={"Accept": "application/json"},
        timeout=75,
    )


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("response", "reply", "message", "text", "content", "answer", "output"):
            if key in value:
                result = extract_text(value[key])
                if result:
                    return result
        for nested in value.values():
            result = extract_text(nested)
            if result:
                return result
    if isinstance(value, list):
        return "\n".join(result for item in value if (result := extract_text(item)))
    return ""


def extract_image(value: Any) -> tuple[str | None, bytes | None]:
    if isinstance(value, dict):
        for key, nested in value.items():
            lower = key.lower()
            if isinstance(nested, str) and nested.startswith(("http://", "https://")):
                if any(word in lower for word in ("image", "photo", "picture", "url", "src")):
                    return nested, None
            if isinstance(nested, str) and nested.startswith("data:image/"):
                try:
                    return None, base64.b64decode(nested.split(",", 1)[1])
                except (ValueError, IndexError):
                    pass
            url, raw = extract_image(nested)
            if url or raw:
                return url, raw
    elif isinstance(value, list):
        for nested in value:
            url, raw = extract_image(nested)
            if url or raw:
                return url, raw
    return None, None


def fallback_image_url(prompt: str) -> str:
    cleaned = re.sub(r"\s+", " ", prompt).strip()[:700]
    return f"{IMAGE_FALLBACK_ENDPOINT}{quote(cleaned, safe='')}?width=1024&height=1024&nologo=true"


def send_ai_response(chat_id: int, prompt: str, status_id: int) -> None:
    try:
        response = ai_request(prompt)
        image_url, image_bytes = extract_image(response)
        text = extract_text(response).strip()
        image_request = prompt.lstrip().lower().startswith("/image")
        if image_request and not image_url and not image_bytes:
            image_url = fallback_image_url(prompt.lstrip()[len("/image"):].strip())
        if image_url:
            telegram_call("sendPhoto", {
                "chat_id": str(chat_id),
                "photo": image_url,
                "caption": "Generated image",
            })
        elif image_bytes:
            telegram_upload(
                "sendPhoto", "photo", "streamly-generated.png", image_bytes,
                {"chat_id": str(chat_id), "caption": "Generated image"},
            )
        if image_request:
            edit_message(
                chat_id, status_id,
                f"<b>Image ready</b>\n\n{escape(text[:700] or 'ছবি তৈরি করা হয়েছে।')}",
            )
            return
        edit_message(
            chat_id, status_id,
            f"<b>AI assistant</b>\n\n{escape(text[:MAX_TEXT_LENGTH] or 'Response পাওয়া গেছে।')}",
        )
    except Exception as error:
        log("AI request failed: %s", error)
        edit_message(
            chat_id, status_id,
            "<b>AI assistant</b>\n\nএই মুহূর্তে উত্তর আনা যায়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।",
        )


def welcome_text(first_name: str = "") -> str:
    greeting = f"স্বাগতম, {escape(first_name)}" if first_name else "স্বাগতম"
    return (
        f"<b>{greeting} — Streamly</b>\n\n"
        "TikTok, Facebook, Instagram ও YouTube-এর public video থেকে "
        "শুধু MP4 video তৈরি করে Telegram-এ পাঠাতে পারবেন।\n\n"
        "<i>শুরু করতে নিচের একটি mode বেছে নিন।</i>"
    )


def help_text() -> str:
    return (
        "<b>Streamly কীভাবে ব্যবহার করবেন</b>\n\n"
        "১. <b>Video Downloader</b> চাপুন\n"
        "২. TikTok / Facebook / Instagram / YouTube বেছে নিন\n"
        "৩. Public video URL পাঠান\n"
        "৪. Quality বেছে নিন\n"
        "৫. Quality বেছে নিলে সরাসরি MP4 video download হবে\n\n"
        f"সীমা: সর্বোচ্চ {MAX_VIDEO_SECONDS // 60} মিনিট এবং "
        f"{format_bytes(MAX_MEDIA_BYTES)}-এর মধ্যে ফাইল।\n\n"
        "Private, paid, age-restricted বা sign-in-only content bypass করা হয় না। "
        "শুধু নিজের বা অনুমোদিত content download করুন।"
    )


def process_message(message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    first_name = (message.get("from") or {}).get("first_name", "")
    if not text:
        send_message(chat_id, "Text URL পাঠান অথবা নিচের menu ব্যবহার করুন।", reply_markup=main_keyboard())
        return

    command = text.split(maxsplit=1)[0].lower()
    if command == "/start":
        CHAT_MODES[chat_id] = "home"
        send_message(chat_id, welcome_text(first_name), reply_markup=main_keyboard())
        return
    if command == "/help":
        send_message(chat_id, help_text(), reply_markup=main_keyboard())
        return
    if command in {"/cancel", "/stop"}:
        CHAT_MODES[chat_id] = "home"
        with STATE_LOCK:
            DOWNLOAD_OPTIONS.pop(chat_id, None)
            ACTIVE_CHATS.discard(chat_id)
        send_message(chat_id, "Cancelled. আবার শুরু করতে পারেন।", reply_markup=main_keyboard())
        return
    if command == "/download":
        CHAT_MODES[chat_id] = "platform"
        send_message(chat_id, "কোন platform-এর video download করবেন?", reply_markup=platform_keyboard())
        return
    if command == "/ai":
        prompt = text[len(command):].strip()
        CHAT_MODES[chat_id] = "ai"
        if prompt:
            status = send_message(chat_id, "💭 Thinking....")
            EXECUTOR.submit(send_ai_response, chat_id, prompt, status["message_id"])
        else:
            send_message(chat_id, "AI mode চালু। আপনার প্রশ্ন লিখুন।")
        return
    if command == "/image":
        prompt = text[len(command):].strip()
        if not prompt:
            send_message(chat_id, "এভাবে লিখুন:\n<code>/image a futuristic city at night</code>")
            return
        status = send_message(chat_id, "AI image তৈরি করছি…")
        EXECUTOR.submit(send_ai_response, chat_id, f"/image {prompt}", status["message_id"])
        return

    mode = CHAT_MODES.get(chat_id, "home")
    url = normalize_url(text)
    detected = platform_from_url(url or "")

    # Pasting a supported URL directly starts its download flow.
    if detected and mode in {"home", "ai"}:
        CHAT_PLATFORMS[chat_id] = detected.key
        mode = "awaiting_url"

    if mode == "platform":
        send_message(chat_id, "একটি platform বেছে নিন।", reply_markup=platform_keyboard())
        return

    if mode == "awaiting_url":
        if not url:
            send_message(chat_id, "Valid public URL পাঠান।", reply_markup=platform_keyboard())
            return

        selected_key = CHAT_PLATFORMS.get(chat_id)
        platform = PLATFORM_BY_KEY.get(selected_key or "")
        detected = platform_from_url(url)

        # If the user pasted a URL from another supported platform, automatically
        # switch to that platform rather than rejecting a perfectly valid URL.
        if detected:
            platform = detected
            CHAT_PLATFORMS[chat_id] = detected.key

        if not detected:
            send_message(
                chat_id,
                "এই URL supported platform-এর public video URL মনে হচ্ছে না।",
                reply_markup=platform_keyboard(),
            )
            return
        platform = detected
        CHAT_PLATFORMS[chat_id] = detected.key

        with STATE_LOCK:
            if chat_id in ACTIVE_CHATS:
                send_message(chat_id, "আপনার আগের download এখনও চলছে। একটু অপেক্ষা করুন।")
                return
            ACTIVE_CHATS.add(chat_id)

        status = send_message(
            chat_id,
            f"<b>{platform.emoji} {escape(platform.title)}</b>\n\nDownload শুরু করছি…",
        )
        CHAT_MODES[chat_id] = "home"
        EXECUTOR.submit(resolve_download, chat_id, status["message_id"], url, platform)
        return

    if mode == "ai":
        status = send_message(chat_id, "💭 Thinking....")
        EXECUTOR.submit(send_ai_response, chat_id, text, status["message_id"])
        return

    send_message(
        chat_id,
        "একটি mode বেছে নিন—Video Downloader বা AI assistant।",
        reply_markup=main_keyboard(),
    )


def process_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    answer_callback(callback_id)
    if chat_id is None or message_id is None:
        return

    if data == "home":
        CHAT_MODES[chat_id] = "home"
        edit_message(chat_id, message_id, welcome_text(), reply_markup=main_keyboard())

    elif data == "help":
        edit_message(chat_id, message_id, help_text(), reply_markup=main_keyboard())

    elif data == "cancel":
        CHAT_MODES[chat_id] = "home"
        with STATE_LOCK:
            DOWNLOAD_OPTIONS.pop(chat_id, None)
            ACTIVE_CHATS.discard(chat_id)
        edit_message(chat_id, message_id, "Cancelled. আবার শুরু করতে পারেন.", reply_markup=main_keyboard())

    elif data == "mode:download":
        CHAT_MODES[chat_id] = "platform"
        edit_message(
            chat_id, message_id,
            "কোন platform-এর video download করবেন?",
            reply_markup=platform_keyboard(),
        )

    elif data == "mode:ai":
        CHAT_MODES[chat_id] = "ai"
        edit_message(chat_id, message_id, "AI assistant mode চালু। আপনার প্রশ্ন লিখুন।")

    elif data.startswith("platform:"):
        key = data.split(":", 1)[1]
        platform = PLATFORM_BY_KEY.get(key)
        if not platform:
            edit_message(chat_id, message_id, "Unsupported platform.", reply_markup=platform_keyboard())
            return
        CHAT_MODES[chat_id] = "awaiting_url"
        CHAT_PLATFORMS[chat_id] = key
        edit_message(
            chat_id,
            message_id,
            f"<b>{platform.emoji} {escape(platform.title)} selected</b>\n\n"
            "এখন public video URL পাঠান।",
            reply_markup=inline_keyboard([
                [{"text": "Change platform", "callback_data": "mode:download"}],
                [{"text": "Cancel", "callback_data": "cancel"}],
            ]),
        )

    elif data == "back:quality":
        item = DOWNLOAD_OPTIONS.get(chat_id)
        if not item or time.time() - float(item.get("created_at", 0)) > STATE_TTL_SECONDS:
            edit_message(
                chat_id, message_id,
                "Quality options expired। আবার link পাঠান.",
                reply_markup=platform_keyboard(),
            )
            return
        CHAT_MODES[chat_id] = "quality"
        edit_message(
            chat_id, message_id,
            f"<b>🎚 Quality নির্বাচন করুন</b>\n\n{escape(item['title'][:100])}",
            reply_markup=quality_keyboard(item["options"]),
        )

    elif data.startswith("quality:"):
        item = DOWNLOAD_OPTIONS.get(chat_id)
        try:
            index = int(data.split(":", 1)[1])
            option = item["options"][index] if item else None
        except (ValueError, IndexError, TypeError, KeyError):
            option = None
        if not option:
            edit_message(
                chat_id, message_id,
                "এই quality selection-টি আর active নেই। আবার URL দিন.",
                reply_markup=platform_keyboard(),
            )
            return
        option = dict(option)
        option["title"] = item["title"]
        option["source_url"] = item["source_url"]
        option["platform_title"] = PLATFORM_BY_KEY.get(item.get("platform", ""), YOUTUBE).title
        option["platform"] = item.get("platform", "")
        CHAT_MODES[chat_id] = "home"
        edit_message(
            chat_id, message_id,
            f"<b>✅ {escape(option['label'])}</b>\n\n🎬 MP4 video download শুরু হচ্ছে…",
        )
        EXECUTOR.submit(
            send_download,
            chat_id,
            message_id,
            item["source_url"],
            option,
        )


def polling_loop() -> None:
    offset = 0
    backoff = 2
    while not SHUTDOWN.is_set():
        try:
            updates = telegram_call(
                "getUpdates",
                {
                    "offset": str(offset),
                    "timeout": "25",
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
            )
            backoff = 2
            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    if update.get("callback_query"):
                        process_callback(update["callback_query"])
                    elif update.get("message"):
                        process_message(update["message"])
                except Exception:
                    LOG.exception("Update handling failed")
            cleanup_state()
        except Exception as error:
            log("Polling error: %s", error)
            SHUTDOWN.wait(backoff)
            backoff = min(backoff * 2, 30)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/healthz", "/health"}:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps({
            "ok": True,
            "service": "streamly",
            "platforms": list(PLATFORM_BY_KEY.keys()),
            "active_downloads": len(ACTIVE_CHATS),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_health_server() -> ThreadingHTTPServer:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    log("Health server listening on 0.0.0.0:%s", port)
    return server


KEEP_ALIVE_MIN_SECONDS = 12 * 60
KEEP_ALIVE_MAX_SECONDS = 14 * 60


def _keep_alive_url() -> str | None:
    explicit = os.getenv("KEEP_ALIVE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/healthz"
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if render_url:
        return render_url.rstrip("/") + "/healthz"
    return None


def keep_alive_loop() -> None:
    url = _keep_alive_url()
    if not url:
        log("Keep-alive disabled: no RENDER_EXTERNAL_URL or KEEP_ALIVE_URL found.")
        return
    log("Keep-alive enabled — pinging every ~12-14 minutes")
    while not SHUTDOWN.wait(random.uniform(KEEP_ALIVE_MIN_SECONDS, KEEP_ALIVE_MAX_SECONDS)):
        try:
            status, _, _ = http_request(url, method="GET", timeout=20, max_bytes=4096)
            log("Keep-alive ping ok (HTTP %s)", status)
        except Exception as error:
            log("Keep-alive ping failed: %s", error)


def start_keep_alive() -> None:
    thread = threading.Thread(target=keep_alive_loop, name="keep-alive", daemon=True)
    thread.start()


def shutdown_handler(_signum: int, _frame: Any) -> None:
    log("Shutdown signal received")
    SHUTDOWN.set()


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TOKEN secret is missing")
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    health_server = start_health_server()
    start_keep_alive()
    try:
        telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
        me = telegram_call("getMe")
        log("Bot connected as @%s", me.get("username", "unknown"))
        polling_thread = threading.Thread(
            target=polling_loop,
            name="telegram-polling",
            daemon=True,
        )
        polling_thread.start()
        while not SHUTDOWN.wait(1):
            pass
    finally:
        SHUTDOWN.set()
        health_server.shutdown()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
        log("Streamly stopped")


if __name__ == "__main__":
    main()
