import json
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from google import genai
from yt_dlp import YoutubeDL

MODEL_NAME = "gemini-3.8-flash"
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A"
LAST_VIDEO_PATH = "last_video.txt"

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


def get_channel_video_status(channel_id: str) -> dict[str, bool]:
    # Flat public listings avoid playback requests, which YouTube blocks on CI IPs.
    # Query both tabs explicitly; Shorts are on a separate tab and never requested.
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
                duration = entry.get("duration")
                ready = (
                    entry.get("live_status") not in {"is_live", "is_upcoming", "post_live"}
                    and isinstance(duration, (int, float))
                    and duration > 180
                )
                status[entry["id"]] = ready
    return status


def get_latest_longform_video_id(channel_id: str) -> str:
    response = requests.get(
        "https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, timeout=30
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    candidates = []
    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        published = entry.findtext("atom:published", namespaces=ns)
        if not video_id or not published:
            raise RuntimeError("Incomplete YouTube feed entry")
        date = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise RuntimeError("Missing publication timezone")
        if date <= datetime.now(timezone.utc):
            candidates.append((date, video_id))
    status = get_channel_video_status(channel_id)
    for _, video_id in sorted(candidates, reverse=True):
        if status.get(video_id, False):
            print(f"Latest completed long-form video: {video_id}")
            return video_id
    raise RuntimeError("No completed long-form videos in YouTube's recent feed")


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


def summarize_transcript(title: str, transcript: str, gemini_key: str) -> str:
    client = genai.Client(api_key=gemini_key)

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
        resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        partials.append(resp.text or "")

    final_prompt = (
        "Combine the chunk summaries into one clean, detailed summary.\n"
        "Focus ONLY on what Chris Camillo says or implies.\n"
        "Use concise bullet points. Group by themes if helpful.\n\n"
        f"Video title: {title}\n\n"
        "Chunk summaries:\n" + "\n\n".join(partials)
    )
    final = client.models.generate_content(model=MODEL_NAME, contents=final_prompt)
    return (final.text or "").strip()


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

    transcript = get_transcript_text(supadata_key, latest_video_id)
    if not transcript:
        raise RuntimeError("Transcript is empty; leaving video pending for the next run.")
    summary = summarize_transcript(title, transcript, gemini_key)
    if not summary:
        raise RuntimeError("Summary is empty; leaving video pending for the next run.")

    message = f"🚀 New Dumb Money Live summary\n\nLink: {link}\n\nChris Camillo summary:\n{summary}"

    telegram_send(telegram_token, telegram_chat_id, message)
    write_last_video_id(latest_video_id)
    print("Sent summary + updated last_video.txt")


if __name__ == "__main__":
    main()
