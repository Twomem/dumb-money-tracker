# dumb-money-tracker

Automated summaries of Dumb Money Live videos that focus on Chris Camillo, delivered
to Telegram via a GitHub Actions cron job.

## How it works

1. A GitHub Actions workflow runs every 8 hours.
2. The workflow checks Supadata's regular-upload and livestream tabs separately,
   excludes Shorts, and compares publication dates to select the newest episode.
3. If a new video is found, the transcript is fetched with Supadata and summarized with Gemini.
4. The summary is sent to Telegram and the last processed video ID is saved.

## Required secrets

Set these GitHub Actions secrets:

- `GEMINI_API_KEY`
- `SUPADATA_API_KEY`
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
FORCE_RUN=true python main.py
```

## Manually trigger a summary in GitHub Actions

Use the workflow "Run workflow" button and set the `force` input to `true` to
send a summary of the latest video on demand.

Changes to the tracker code or workflow on `main` also trigger a check. Runs are
serialized to avoid overlapping Telegram sends. Empty transcripts or summaries
fail the run without advancing `last_video.txt`, allowing a later retry.
