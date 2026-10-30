# dumb-money-tracker

Automated summaries of Dumb Money Live videos that focus on Chris Camillo, delivered
to Telegram via a GitHub Actions cron job.

## How it works

1. A GitHub Actions workflow runs every 8 hours.
2. The workflow checks YouTube's public uploads playlist (flat listing, no playback
   and no YouTube Data API) and selects the newest completed long-form episode,
   excluding Shorts, unfinished live streams, and clips of three minutes or less.
3. If a new video is found, the transcript is fetched with Supadata and summarized with
   a free Gemini model. If that model is overloaded, a second free model is tried once.
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

Video discovery uses YouTube's public uploads playlist via a flat `yt-dlp` listing
(no playback requests and no YouTube Data API key). It costs no Supadata credits.
The channel Atom feed is not used: it only keeps about 15 mixed uploads, so a
shorts-only stretch hid every long episode and failed the job, and that endpoint
also returns HTTP 404 for hours at a time. Completed videos longer than three
minutes are eligible; this excludes Shorts and other short clips. Live, upcoming,
and still-processing streams are skipped and rechecked later. Only the latest
eligible video in the recent uploads window is processed; it is not a historical
backfill.

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

Summaries use the free Gemini API with the existing `GEMINI_API_KEY`. The primary
model is `gemini-3.5-flash-lite`, which has a large free daily allowance.
`gemini-3.1-flash-lite` is tried once if the primary model is overloaded, rate
limited, or returns nothing. Each request has a 45 second timeout and no SDK retry
loop, so a 503 does not burn the job's Actions minutes. `gemini-3.8-flash` stays
free but was failing these runs with 503 high-demand errors and a much smaller
free daily cap, which chunked summaries can exhaust. A failed or empty summary does
not advance `last_video.txt`, so the same video is retried on the next run. A
summary that was produced but not delivered is cached and reused, so the retry
does not spend another Gemini request. Gemini availability errors can still delay
delivery until the next run, but they no longer require refetching a cached transcript.
