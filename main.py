import os
import requests
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai  # <--- New Google library
from google.genai import types

# Configuration
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A"

def get_latest_video():
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    feed = feedparser.parse(feed_url)
    if feed.entries:
        return feed.entries[0].title, feed.entries[0].link, feed.entries[0].id.split(':')[-1]
    return None, None, None

def get_summary(transcript_text, title):
    # Updated to the 2026 'google-genai' Client pattern
    client = genai.Client(api_key=GEMINI_KEY)
    
    prompt = f"Video: {title}\nTranscript: {transcript_text}\n\nSummarize Chris Camillo's specific trade ideas and bold all tickers."
    
    response = client.models.generate_content(
        model="gemini-2.5-flash", # Latest stable model
        contents=prompt
    )
    return response.text

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(text), 4000):
        part = text[i:i+4000]
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": part, "parse_mode": "Markdown"})

def main():
    title, link, video_id = get_latest_video()
    
    if os.path.exists("last_video.txt"):
        with open("last_video.txt", "r") as f:
            if f.read().strip() == video_id:
                print("No new video found.")
                return

    try:
        # 1. Create a tool instance (The "New" Way)
        ytt_api = YouTubeTranscriptApi() 
        
        # 2. Get the list of available transcripts
        transcript_list = ytt_api.list(video_id)
        
        # 3. Find the English one and fetch it
        transcript = transcript_list.find_transcript(['en']).fetch()
        full_text = " ".join([t['text'] for t in transcript])
        
        # 4. Pass to Gemini for analysis
        summary = get_summary(full_text, title)
        
        # 5. Notify via Telegram
        msg = f"🚀 *New Alpha*\n\n*Video:* {title}\n\n{summary}\n\n[Watch]({link})"
        send_telegram(msg)
        
        # 6. Save video ID so we don't repeat
        with open("last_video.txt", "w") as f:
            f.write(video_id)
            
    except Exception as e:
        print(f"Error logic: {e}")

if __name__ == "__main__":
    main()
