# dumb-money-tracker

Automated summaries of Dumb Money Live videos that focus on Chris Camillo, delivered
to Telegram via a GitHub Actions cron job.

## How it works

1. A GitHub Actions workflow runs every 8 hours.
2. The workflow checks the channel RSS feed for a new video.
3. If a new video is found, the transcript is fetched (Supadata if configured, otherwise
   `youtube_transcript_api`) and summarized with Gemini.
4. The summary is sent to Telegram and the last processed video ID is saved.

## Required secrets

Set these GitHub Actions secrets:

- `GEMINI_API_KEY`
- `SUPADATA_API_KEY` (optional, uses Supadata to fetch transcripts)
- `SUPADATA_VIDEO_PARAM` (optional, default `videoId`; use `url` for full YouTube URLs)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Local run

Install dependencies and run:

```bash
pip install -r requirements.txt
python main.py
```

To force a summary even if the latest video was already processed:

```bash
python main.py --force
```

## Manually trigger a summary in GitHub Actions

Use the workflow "Run workflow" button and set the `force` input to `true` to
send a summary of the latest video on demand.
