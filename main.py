import argparse
import os
from typing import Iterable

import feedparser
import requests
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi

# Configuration
MODEL_NAME = "gemini-2.5-flash"
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A"
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
LAST_VIDEO_PATH = "last_video.txt"
TELEGRAM_MESSAGE_LIMIT = 4000
SUPADATA_DEFAULT_URL = "https://api.supadata.ai/v1/youtube/transcript"


def get_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize the latest Dumb Money Live video and send to Telegram."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send a summary even if the latest video was already processed.",
    )
    return parser.parse_args()


def get_latest_video(feed_url: str) -> tuple[str | None, str | None, str | None]:
    feed = feedparser.parse(feed_url)
    if not feed.entries:
        return None, None, None
    entry = feed.entries[0]
    video_id = entry.id.split(":")[-1]
    return entry.title, entry.link, video_id


def read_last_video_id(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        value = handle.read().strip()
    return value or None


def write_last_video_id(path: str, video_id: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(video_id)


def fetch_transcript_text(video_id: str) -> str:
    transcript_api = YouTubeTranscriptApi()
    transcript_list = transcript_api.list(video_id)
    try:
        transcript = transcript_list.find_transcript(["en"])
    except Exception:
        transcript = transcript_list.find_generated_transcript(["en"])
    entries = transcript.fetch()
    return " ".join(entry["text"] for entry in entries)


def _normalize_transcript_payload(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return " ".join(_extract_transcript_text(item) for item in payload).strip()
    if isinstance(payload, dict):
        if "text" in payload:
            return str(payload["text"])
        for key in ("transcript", "data", "result", "items", "entries"):
            if key in payload:
                return _normalize_transcript_payload(payload[key])
        for key in ("transcript_text", "transcriptText", "content"):
            if key in payload:
                value = payload[key]
                if key == "content" and isinstance(value, list):
                    return " ".join(_extract_transcript_text(item) for item in value).strip()
                return str(value)
    raise ValueError("Supadata response did not include transcript text.")


def _extract_transcript_text(item: object) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if "text" in item:
            return str(item["text"])
        if "transcript" in item:
            return _normalize_transcript_payload(item["transcript"])
    return str(item)


def fetch_transcript_text_supadata(video_id: str, api_key: str) -> str:
    base_url = os.environ.get("SUPADATA_BASE_URL", SUPADATA_DEFAULT_URL)
    # Supadata expects `videoId` (camelCase) or `url` as the query parameter.
    param_name = os.environ.get("SUPADATA_VIDEO_PARAM", "videoId")
    headers = {
        "x-api-key": api_key,
    }
    response = requests.get(
        base_url,
        headers=headers,
        params={param_name: video_id, "text": "true"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return _normalize_transcript_payload(payload)


def chunk_text(text: str, max_chars: int = 12000, overlap: int = 600) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        if current_len + len(word) + 1 > max_chars and current:
            chunk = " ".join(current)
            chunks.append(chunk)
            overlap_words = chunk.split()[-overlap:] if overlap else []
            current = list(overlap_words)
            current_len = sum(len(w) + 1 for w in current)
        current.append(word)
        current_len += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def build_chunk_prompt(title: str, chunk: str, index: int, total: int) -> str:
    return (
        "You are summarizing a long YouTube transcript. Focus ONLY on what "
        "Chris Camillo says or directly implies.\n\n"
        f"Video title: {title}\n"
        f"Chunk {index} of {total}\n\n"
        "Instructions:\n"
        "- Summarize Chris's statements, ideas, and any trade theses.\n"
        "- Ignore other speakers unless they directly respond to Chris.\n"
        "- Keep the summary detailed but concise.\n\n"
        f"Transcript chunk:\n{chunk}"
    )


def build_final_prompt(title: str, chunk_summaries: Iterable[str]) -> str:
    combined = "\n\n".join(chunk_summaries)
    return (
        "You are producing the final detailed summary of a YouTube video.\n"
        "Focus ONLY on what Chris Camillo says or implies.\n\n"
        f"Video title: {title}\n\n"
        "Instructions:\n"
        "- Provide a detailed summary of Chris's views and reasoning.\n"
        "- Highlight any trade ideas, catalysts, or actionable insights.\n"
        "- Use bullet points for clarity.\n\n"
        f"Chunk summaries:\n{combined}"
    )


def summarize_with_gemini(client: genai.Client, prompt: str) -> str:
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return response.text or ""


def generate_summary(title: str, transcript_text: str, client: genai.Client) -> str:
    chunks = chunk_text(transcript_text)
    chunk_summaries = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = build_chunk_prompt(title, chunk, index, len(chunks))
        chunk_summaries.append(summarize_with_gemini(client, prompt))
    final_prompt = build_final_prompt(title, chunk_summaries)
    return summarize_with_gemini(client, final_prompt)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for start in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        part = text[start : start + TELEGRAM_MESSAGE_LIMIT]
        requests.post(url, data={"chat_id": chat_id, "text": part}, timeout=30)


def build_telegram_message(title: str, link: str, summary: str) -> str:
    return (
        "🚀 New Dumb Money Live summary\n\n"
        f"Video: {title}\n"
        f"Link: {link}\n\n"
        "Chris Camillo summary:\n"
        f"{summary}"
    )


def should_process_video(*, force_run: bool, last_video_id: str | None, video_id: str) -> bool:
    if force_run:
        return True
    return last_video_id != video_id


def main() -> None:
    args = parse_args()
    try:
        gemini_key = get_env_var("GEMINI_API_KEY")
        telegram_token = get_env_var("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = get_env_var("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        print(exc)
        return
    supadata_api_key = os.environ.get("SUPADATA_API_KEY")

    title, link, video_id = get_latest_video(FEED_URL)
    if not video_id or not title or not link:
        print("No video entries found in feed.")
        return

    last_video_id = read_last_video_id(LAST_VIDEO_PATH)
    force_run = args.force or os.environ.get("FORCE_RUN", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if not should_process_video(
        force_run=force_run, last_video_id=last_video_id, video_id=video_id
    ):
        print("No new video found.")
        return

    try:
        if supadata_api_key:
            transcript_text = fetch_transcript_text_supadata(video_id, supadata_api_key)
        else:
            transcript_text = fetch_transcript_text(video_id)
        client = genai.Client(api_key=gemini_key)
        summary = generate_summary(title, transcript_text, client)
        message = build_telegram_message(title, link, summary)
        send_telegram_message(telegram_token, telegram_chat_id, message)
        write_last_video_id(LAST_VIDEO_PATH, video_id)
        print("Summary sent and last video ID updated.")
    except Exception as exc:
        print(f"Error processing video: {exc}")


if __name__ == "__main__":
    main()
