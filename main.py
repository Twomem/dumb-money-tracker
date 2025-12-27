import os
import requests
import feedparser
import smtplib
# from email.message import EmailMessage
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai

# --- Configuration (Pulled from GitHub Secrets) ---
CHANNEL_ID = "UCS01CiRDAiyhR_mTHXDW23A" 
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
WHATSAPP_PHONE = os.environ.get('WHATSAPP_PHONE')
WHATSAPP_KEY = os.environ.get('WHATSAPP_KEY')
# EMAIL_ADDRESS = os.environ.get('EMAIL_ADDRESS')
# EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD') # Gmail App Password
# RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL')

def get_latest_video():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries: return None, None
    return feed.entries[0].id.split(":")[-1], feed.entries[0].title

def get_summary(transcript_text, title):
    genai.configure(api_key=GEMINI_KEY)
    model = genai.GenerativeModel('gemini-3-flash-preview')
    prompt = f"""
    Video Title: {title}
    Transcript: {transcript_text}
    
    TASK: Provide a detailed summary of this video focusing on what Chris Camillo said. 
    1. Identify his primary investment thesis for this video.
    2. List every ticker (e.g., $AAPL, $TSLA) he mentioned and his sentiment (Bullish/Bearish).
    3. Detail his reasoning
    4. Ignore Dave and Jordan unless they are directly debating Chris's specific trade.
    Format: Use bullet points. Use *bold* for tickers and key takes.
    """
    response = model.generate_content(prompt)
    return response.text

def send_whatsapp(message):
    # CallMeBot API expects URL-encoded text
    url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={requests.utils.quote(message)}&apikey={WHATSAPP_KEY}"
    r = requests.get(url)
    return r.status_code

# def send_email(subject, body):
#    msg = EmailMessage()
#    msg.set_content(body)
#    msg['Subject'] = subject
#    msg['From'] = EMAIL_ADDRESS
#    msg['To'] = RECIPIENT_EMAIL
#    
#    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
#        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
#        smtp.send_message(msg)

# --- Main Execution ---
video_id, title = get_latest_video()

# Check against last processed video
if os.path.exists("last_video.txt"):
    with open("last_video.txt", "r") as f:
        if f.read().strip() == video_id:
            print("No new video found.")
            exit()

try:
    print(f"Processing new video: {title}")
    transcript = YouTubeTranscriptApi.get_transcript(video_id)
    text = " ".join([i['text'] for i in transcript])
    
    summary = get_summary(text, title)
    
    # 1. Send WhatsApp
    wa_status = send_whatsapp(f"*Dumb Money Chris-Only Summary*\n{title}\n\n{summary}")
    print(f"WhatsApp sent (Status: {wa_status})")
    
    # 2. Send Email
    # send_email(f"Dumb Money Summary: {title}", summary)
    # print("Email sent.")
    
    # 3. Save progress
    with open("last_video.txt", "w") as f:
        f.write(video_id)

except Exception as e:
    print(f"Error: {e}")
