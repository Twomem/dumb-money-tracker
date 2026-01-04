import os
import requests
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai

# Configuration from GitHub Secrets
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A" # Dumb Money Live

def get_latest_video():
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    feed = feedparser.parse(feed_url)
    if feed.entries:
        return feed.entries[0].title, feed.entries[0].link, feed.entries[0].id.split(':')[-1]
    return None, None, None

def get_summary(transcript_text, title):
    genai.configure(api_key=GEMINI_KEY)
    # Using Gemini 3 Flash (Dec 2025) for PhD-level reasoning
    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    prompt = f"""
    Video: {title}
    Transcript: {transcript_text}
    
    TASK: Extract high-conviction trade ideas specifically from Chris Camillo.
    FORMAT: 
    - [CHRIS'S ALPHA]: Summarize his specific observations and thesis.
    - [TICKERS]: Bold all tickers (e.g., **$TSLA**).
    - [SENTIMENT]: Bullish/Bearish/Watchlist.
    """
    response = model.generate_content(prompt)
    return response.text

def send_telegram(text):
    # Telegram has a 4096 character limit per message
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(text), 4000):
        part = text[i:i+4000]
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": part, "parse_mode": "Markdown"})

def main():
    title, link, video_id = get_latest_video()
    
    # Check if we've already processed this video
    if os.path.exists("last_video.txt"):
        with open("last_video.txt", "r") as f:
            if f.read().strip() == video_id:
                print("No new video found.")
                return

    try:
        # 1. Get Transcript
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        full_text = " ".join([t['text'] for t in transcript])
        
        # 2. AI Analysis
        summary = get_summary(full_text, title)
        
        # 3. Notify
        msg = f"🚀 *New Alpha Detected*\n\n*Video:* {title}\n\n{summary}\n\n[Watch Video]({link})"
        send_telegram(msg)
        
        # 4. Save progress
        with open("last_video.txt", "w") as f:
            f.write(video_id)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
