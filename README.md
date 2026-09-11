# dumb-money-tracker

Automated summaries of Dumb Money Live videos that focus on Chris Camillo, delivered
to Telegram via a GitHub Actions cron job.

## How it works

1. A GitHub Actions workflow runs every 8 hours.
2. The workflow checks YouTube's free feed and video metadata to select the newest
   completed long-form episode, excluding clips of three minutes or less.
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

## Staying within Supadata's free plan

Video discovery uses YouTube's public Atom feed (uploads and livestreams), with
flat public-channel `yt-dlp` listings (no playback requests). It costs no Supadata credits and needs no new key.
Completed videos longer than three minutes are eligible; this conservatively
excludes Shorts and other short clips. Live/upcoming/processing streams are
rechecked later. Only the latest eligible video in the feed is processed, as
before; the feed is a recent window, not a historical backfill mechanism.

Before every uncached transcript request, `/v1/me` checks actual account-wide
billing-period usage. The tracker refuses a request that would exceed 95 credits
or leave fewer than five plan credits. Invalid/unavailable usage information
stops processing safely. The provider's billing-period reset automatically
restores eligibility, including when other apps share the account. This is a
pre-request guard, not an atomic account-wide spending lock: leave Auto Recharge
off, especially if other apps also use the account.

Transcripts use `mode=native` (one credit; no AI-generation fallback). Successful
transcripts are saved before Gemini runs. GitHub Actions restores `.tracker-cache`
and saves it even after a failed summary, so retries normally reuse the transcript.
The cache contains public-video text only, never API keys. GitHub
can evict caches; after eviction a transcript may need fetching again, subject to
the same credit guard. Missing transcripts may consume a credit on each attempt.
Force runs reuse cached transcripts and cannot bypass the budget.

The Gemini model and Telegram destination are unchanged. Gemini availability
errors can still delay delivery, but no longer require refetching a cached transcript.
