import json
import math
import os
import re
from pathlib import Path

import requests
from google import genai
from google.genai import errors as genai_errors
from yt_dlp import YoutubeDL

# gemini-3.8-flash is free but capped near 20 requests/day and has been returning
# 503 UNAVAILABLE ("high demand"). Flash-Lite models stay on the free tier and
# have a much larger daily allowance, so a transcript still fits when Flash is down.
MODEL_NAME = "gemini-3.5-flash-lite"
FALLBACK_MODEL_NAMES = ("gemini-3.1-flash-lite",)
# HttpOptions.timeout is milliseconds. One attempt each: the SDK's default wait
# has no timeout, and a single hung generate_content burned the whole job.
GEMINI_TIMEOUT_MS = 45_000
GEMINI_MAX_OUTPUT_TOKENS = 4096
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A"
LAST_VIDEO_PATH = "last_video.txt"
# Longer than three minutes. This also drops Shorts and anything under two minutes.
MIN_LONGFORM_SECONDS = 180
UNREADY_LIVE = {"is_live", "is_upcoming", "post_live"}
# The channel posts several Shorts a day. 60 uploads still reaches the latest
# long-form episode after a shorts-only week.
UPLOADS_LOOKBACK = 60

SUPADATA_TRANSCRIPT_URL = "https://api.supadata.ai/v1/transcript"
SUPADATA_ACCOUNT_URL = "https://api.supadata.ai/v1/me"
CACHE_DIR = Path(".tracker-cache")
CREDIT_LIMIT = 95

TELEGRAM_MESSAGE_LIMIT = 4000


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise ValueError(f"Missing required env var: {name}")
    return v


def read_last_video_id() -> str | None:
    if not os.path.exists(LAST_VIDEO_PATH):
        return None
    v = open(LAST_VIDEO_PATH, "r", encoding="utf-8").read().strip()
    return v or None


def write_last_video_id(video_id: str) -> None:
    with open(LAST_VIDEO_PATH, "w", encoding="utf-8") as f:
        f.write(video_id)


def cache_path(kind: str, video_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video ID")
    return CACHE_DIR / kind / f"{video_id}.json"


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def entry_is_ready(entry: dict) -> bool:
    duration = entry.get("duration")
    return (
        entry.get("live_status") not in UNREADY_LIVE
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration > MIN_LONGFORM_SECONDS
    )


def get_channel_video_status(channel_id: str) -> dict[str, bool]:
    # Flat public listings avoid playback requests, which YouTube blocks on CI IPs.
    # Query both tabs explicitly; Shorts are on a separate tab and never requested.
    # Scheduled discovery uses the uploads playlist; this helper keeps the same rule.
    status = {}
    with YoutubeDL(
        {"quiet": True, "extract_flat": True, "playlistend": 20, "socket_timeout": 30}
    ) as youtube:
        for tab in ("videos", "streams"):
            info = youtube.extract_info(
                f"https://www.youtube.com/channel/{channel_id}/{tab}", download=False
            )
            if not info or "entries" not in info:
                raise RuntimeError(f"YouTube did not return the {tab} listing")
            for entry in info["entries"]:
                status[entry["id"]] = entry_is_ready(entry)
    return status


def uploads_playlist_id(channel_id: str) -> str:
    # YouTube's public uploads playlist is the channel id with UC replaced by UU.
    if not (channel_id.startswith("UC") and len(channel_id) > 2):
        raise ValueError("Invalid YouTube channel ID")
    return "UU" + channel_id[2:]


def get_latest_longform_video_id(channel_id: str) -> str:
    # The channel Atom feed only keeps about 15 items and mixes in Shorts, and it
    # returns 404 for hours at a time. The uploads playlist is newest-first and
    # includes duration, so Shorts and unfinished streams can be skipped without it.
    playlist_id = uploads_playlist_id(channel_id)
    with YoutubeDL(
        {
            "quiet": True,
            "extract_flat": True,
            "playlistend": UPLOADS_LOOKBACK,
            "socket_timeout": 30,
        }
    ) as youtube:
        info = youtube.extract_info(
            f"https://www.youtube.com/playlist?list={playlist_id}", download=False
        )
    if not info or "entries" not in info:
        raise RuntimeError("YouTube did not return the uploads listing")
    for entry in info["entries"]:
        if isinstance(entry, dict) and entry.get("id") and entry_is_ready(entry):
            video_id = entry["id"]
            print(f"Latest completed long-form video: {video_id}")
            return video_id
    raise RuntimeError("No completed long-form videos in YouTube's recent uploads")


def check_credit_budget(supadata_key: str) -> None:
    response = requests.get(SUPADATA_ACCOUNT_URL, headers={"x-api-key": supadata_key}, timeout=30)
    response.raise_for_status()
    data = response.json()
    used, maximum = data.get("usedCredits"), data.get("maxCredits")
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for value in (used, maximum)
    ):
        raise RuntimeError("Cannot verify Supadata credit usage; refusing transcript request")
    ceiling = min(CREDIT_LIMIT, maximum - 5)
    if used + 1 > ceiling:
        raise RuntimeError(
            f"Supadata credit guard: {used:g} used; limit {ceiling:g}. "
            "Waiting for the billing allowance to renew; no transcript requested."
        )
    print(f"Supadata usage: {used:g}/{maximum:g}; tracker ceiling: {ceiling:g}")


def get_transcript_text(supadata_key: str, video_id: str) -> str:
    path = cache_path("transcripts", video_id)
    if path.exists():
        content = json.loads(path.read_text(encoding="utf-8")).get("content")
        if isinstance(content, str) and content.strip():
            print(f"Using cached transcript: {video_id}")
            return content
        raise RuntimeError("Invalid cached transcript; refusing automatic refetch")
    check_credit_budget(supadata_key)
    response = requests.get(
        SUPADATA_TRANSCRIPT_URL,
        headers={"x-api-key": supadata_key},
        params={
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "text": "true",
            "mode": "native",
        },
        timeout=60,
    )
    response.raise_for_status()
    if response.status_code != 200:
        raise RuntimeError("Native transcript not ready; leaving video pending")
    content = response.json().get("content")
    if isinstance(content, list):
        content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Transcript is empty or invalid; leaving video pending")
    content = content.strip()
    # Saved before Gemini/Telegram: later failures must not require another purchase.
    save_json(path, {"content": content})
    return content


def chunk_text(text: str, max_chars: int = 12000) -> list[str]:
    chunks, current = [], []
    n = 0
    for w in text.split():
        if n + len(w) + 1 > max_chars and current:
            chunks.append(" ".join(current))
            current, n = [], 0
        current.append(w)
        n += len(w) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def make_gemini_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options={
            "timeout": GEMINI_TIMEOUT_MS,
            "retry_options": {"attempts": 1},
        },
    )


def should_try_another_model(exc: Exception) -> bool:
    # 404: this model id was removed. 429/5xx and dropped connections are transient.
    # 401/403 are bad credentials and will not succeed on a different free model.
    code = getattr(exc, "code", None)
    if code in {404, 408, 429, 500, 502, 503, 504}:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return type(exc).__name__ in {
        "RemoteProtocolError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "ReadTimeout",
        "WriteTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "NetworkError",
        "ProtocolError",
        "TimeoutException",
    }


def _generate_once(client: genai.Client, model: str, contents: str, max_output_tokens: int):
    # Minimal thinking keeps free-tier calls short. Some models reject that field, and a
    # 400 there must not fail the run, so the same request is sent once without it.
    configs = (
        {
            "max_output_tokens": max_output_tokens,
            "thinking_config": {"thinking_level": "minimal"},
        },
        {"max_output_tokens": max_output_tokens},
    )
    for config in configs:
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except genai_errors.APIError as exc:
            if exc.code == 400 and "thinking_config" in config:
                print(f"{model} rejected the thinking config; retrying without it")
                continue
            raise
    raise RuntimeError(f"{model} did not return a response")


def generate_with_fallback(client: genai.Client, contents: str, max_output_tokens: int) -> str:
    models = (MODEL_NAME, *FALLBACK_MODEL_NAMES)
    for index, model in enumerate(models):
        try:
            response = _generate_once(client, model, contents, max_output_tokens)
        except genai_errors.APIError as exc:
            if index == len(models) - 1 or not should_try_another_model(exc):
                raise
            print(f"{model} unavailable ({exc.code}); trying the next free model")
            continue
        except Exception as exc:
            if index == len(models) - 1 or not should_try_another_model(exc):
                raise
            print(f"{model} request failed ({type(exc).__name__}); trying the next free model")
            continue
        text = (getattr(response, "text", None) or "").strip()
        if text:
            if model != MODEL_NAME:
                print(f"Using fallback model {model}")
            return text
        if index != len(models) - 1:
            print(f"{model} returned empty text; trying the next free model")
    return ""


def load_cached_summary(video_id: str) -> str | None:
    path = cache_path("summaries", video_id)
    if not path.exists():
        return None
    try:
        content = json.loads(path.read_text(encoding="utf-8")).get("content")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if isinstance(content, str) and content.strip():
        print(f"Using cached summary: {video_id}")
        return content.strip()
    return None


def store_summary(video_id: str, summary: str) -> None:
    save_json(cache_path("summaries", video_id), {"content": summary})


def summarize_transcript(title: str, transcript: str, gemini_key: str) -> str:
    client = make_gemini_client(gemini_key)

    chunks = chunk_text(transcript)
    partials = []
    for i, c in enumerate(chunks, start=1):
        prompt = (
            "You are summarizing a long YouTube transcript.\n"
            "Focus ONLY on what Chris Camillo says or directly implies.\n\n"
            f"Video title: {title}\n"
            f"Chunk {i} of {len(chunks)}\n\n"
            "Return bullet points with trade theses, catalysts, and actionable insights.\n\n"
            f"Transcript:\n{c}"
        )
        partial = generate_with_fallback(client, prompt, GEMINI_MAX_OUTPUT_TOKENS)
        if not partial:
            raise RuntimeError("Summary chunk is empty; leaving video pending for the next run.")
        partials.append(partial)

    final_prompt = (
        "Combine the chunk summaries into one clean, detailed summary.\n"
        "Focus ONLY on what Chris Camillo says or implies.\n"
        "Use concise bullet points. Group by themes if helpful.\n\n"
        f"Video title: {title}\n\n"
        "Chunk summaries:\n" + "\n\n".join(partials)
    )
    return generate_with_fallback(client, final_prompt, GEMINI_MAX_OUTPUT_TOKENS)


def telegram_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for start in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        part = text[start : start + TELEGRAM_MESSAGE_LIMIT]
        requests.post(url, data={"chat_id": chat_id, "text": part}, timeout=30).raise_for_status()


def main() -> None:
    gemini_key = env("GEMINI_API_KEY")
    supadata_key = env("SUPADATA_API_KEY")
    telegram_token = env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = env("TELEGRAM_CHAT_ID")

    force = os.environ.get("FORCE_RUN", "").lower() in {"1", "true", "yes"}

    latest_video_id = get_latest_longform_video_id(CHANNEL_ID)
    last_video_id = read_last_video_id()

    if (not force) and last_video_id == latest_video_id:
        print("No new long-form video.")
        return

    # Simple title/link without any YouTube API calls
    link = f"https://www.youtube.com/watch?v={latest_video_id}"
    title = f"Dumb Money Live ({latest_video_id})"

    summary = load_cached_summary(latest_video_id)
    if not summary:
        transcript = get_transcript_text(supadata_key, latest_video_id)
        if not transcript:
            raise RuntimeError("Transcript is empty; leaving video pending for the next run.")
        summary = summarize_transcript(title, transcript, gemini_key)
        if not summary:
            raise RuntimeError("Summary is empty; leaving video pending for the next run.")
        # Saved before Telegram. A send failure must not spend another free-tier summary.
        store_summary(latest_video_id, summary)

    message = f"🚀 New Dumb Money Live summary\n\nLink: {link}\n\nChris Camillo summary:\n{summary}"

    telegram_send(telegram_token, telegram_chat_id, message)
    write_last_video_id(latest_video_id)
    print("Sent summary + updated last_video.txt")


if __name__ == "__main__":
    main()
