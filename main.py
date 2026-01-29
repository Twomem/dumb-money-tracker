import os
import requests
from google import genai

MODEL_NAME = "gemini-2.5-flash"
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A"
LAST_VIDEO_PATH = "last_video.txt"

SUPADATA_CHANNEL_VIDEOS_URL = "https://api.supadata.ai/v1/youtube/channel/videos"
SUPADATA_TRANSCRIPT_URL = "https://api.supadata.ai/v1/youtube/transcript"

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


def get_latest_longform_video_id(supadata_key: str, channel_id: str) -> str:
    # Supadata supports type=video to return ONLY long-form (vertical) videos
    # Response includes { videoIds: [...], shortIds: [...], liveIds: [...] }
    r = requests.get(
        SUPADATA_CHANNEL_VIDEOS_URL,
        headers={"x-api-key": supadata_key},
        params={"id": channel_id, "type": "video", "limit": 5},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    video_ids = data.get("videoIds") or []
    if not video_ids:
        raise RuntimeError("No long-form videos returned by Supadata.")
    return str(video_ids[0])  # latest-first


def get_transcript_text(supadata_key: str, video_id: str) -> str:
    # Supadata youtube transcript supports videoId and text=true (plain text transcript).
    r = requests.get(
        SUPADATA_TRANSCRIPT_URL,
        headers={"x-api-key": supadata_key},
        params={"videoId": video_id, "text": "true"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()

    # Docs say text=true returns plain text, but be robust to either string or chunk list.
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict)).strip()

    raise RuntimeError("Unexpected Supadata transcript response format.")


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

    latest_video_id = get_latest_longform_video_id(supadata_key, CHANNEL_ID)
    last_video_id = read_last_video_id()

    if (not force) and last_video_id == latest_video_id:
        print("No new long-form video.")
        return

    # Simple title/link without any YouTube API calls
    link = f"https://www.youtube.com/watch?v={latest_video_id}"
    title = f"Dumb Money Live ({latest_video_id})"

    transcript = get_transcript_text(supadata_key, latest_video_id)
    summary = summarize_transcript(title, transcript, gemini_key)

    message = (
        "🚀 New Dumb Money Live summary\n\n"
        f"Link: {link}\n\n"
        "Chris Camillo summary:\n"
        f"{summary}"
    )

    telegram_send(telegram_token, telegram_chat_id, message)
    write_last_video_id(latest_video_id)
    print("Sent summary + updated last_video.txt")


if __name__ == "__main__":
    main()
