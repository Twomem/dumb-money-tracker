import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

spec = importlib.util.spec_from_file_location("tracker", Path(__file__).parents[1] / "main.py")
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


def response(data):
    result = Mock()
    result.json.return_value = data
    return result


def feed(entries):
    xml = '<feed xmlns="http://www.w3.org/2005/Atom" '
    xml += 'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
    for video_id, date in entries:
        xml += f"<entry><yt:videoId>{video_id}</yt:videoId><published>{date}</published></entry>"
    result = Mock()
    result.content = (xml + "</feed>").encode()
    return result


def test_feed_sorts_uploads_and_replays_and_skips_shorts(monkeypatch):
    get = Mock(
        return_value=feed(
            [
                ("old", "2026-07-14T00:00:00Z"),
                ("replay", "2026-09-05T00:00:00Z"),
                ("short", "2026-09-06T00:00:00Z"),
            ]
        )
    )
    monkeypatch.setattr(tracker.requests, "get", get)
    ready = Mock(side_effect=lambda v: v != "short")
    monkeypatch.setattr(tracker, "video_is_ready", ready)
    assert tracker.get_latest_longform_video_id("channel") == "replay"
    assert [c.args[0] for c in ready.call_args_list] == ["short", "replay"]
    assert all("supadata" not in c.args[0] for c in get.call_args_list)


def test_discovery_errors_are_not_reported_as_no_new_video(monkeypatch):
    failed = Mock()
    failed.raise_for_status.side_effect = requests.HTTPError("unavailable")
    monkeypatch.setattr(tracker.requests, "get", Mock(return_value=failed))
    with pytest.raises(requests.HTTPError):
        tracker.get_latest_longform_video_id("channel")


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    for key in ("GEMINI_API_KEY", "SUPADATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "test")
    monkeypatch.delenv("FORCE_RUN", raising=False)
    state = tmp_path / "last_video.txt"
    state.write_text("bloom")
    monkeypatch.setattr(tracker, "LAST_VIDEO_PATH", str(state))
    monkeypatch.setattr(tracker, "get_latest_longform_video_id", Mock(return_value="tesla"))
    monkeypatch.setattr(tracker, "get_transcript_text", Mock(return_value="Transcript"))
    monkeypatch.setattr(tracker, "summarize_transcript", Mock(return_value="Summary"))
    monkeypatch.setattr(tracker, "telegram_send", Mock())
    return state


def test_new_replay_sent_once_and_checkpointed(pipeline):
    tracker.main()
    assert pipeline.read_text() == "tesla"
    assert "watch?v=tesla" in tracker.telegram_send.call_args.args[2]
    tracker.main()
    tracker.telegram_send.assert_called_once()
    tracker.get_transcript_text.assert_called_once()


@pytest.mark.parametrize("stage", ["get_transcript_text", "summarize_transcript", "telegram_send"])
def test_failed_processing_keeps_previous_checkpoint(pipeline, stage):
    getattr(tracker, stage).side_effect = RuntimeError("temporary failure")
    with pytest.raises(RuntimeError):
        tracker.main()
    assert pipeline.read_text() == "bloom"


@pytest.mark.parametrize("stage", ["get_transcript_text", "summarize_transcript"])
def test_empty_content_not_sent_or_checkpointed(pipeline, stage):
    getattr(tracker, stage).return_value = ""
    with pytest.raises(RuntimeError, match="empty"):
        tracker.main()
    tracker.telegram_send.assert_not_called()
    assert pipeline.read_text() == "bloom"


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "CACHE_DIR", tmp_path)
    return tmp_path


def test_transcript_reused_across_runs_after_gemini_failure(monkeypatch, cache):
    account = response({"usedCredits": 90, "maxCredits": 100})
    transcript = response({"content": "Saved words"})
    transcript.status_code = 200
    get = Mock(side_effect=[account, transcript])
    monkeypatch.setattr(tracker.requests, "get", get)
    assert tracker.get_transcript_text("key", "abcdefghijk") == "Saved words"
    spec.loader.exec_module(tracker)
    monkeypatch.setattr(tracker, "CACHE_DIR", cache)
    assert tracker.get_transcript_text("key", "abcdefghijk") == "Saved words"
    assert get.call_count == 2
    assert get.call_args.kwargs["params"]["mode"] == "native"


@pytest.mark.parametrize(
    "data",
    [
        {"usedCredits": 95, "maxCredits": 100},
        {"usedCredits": 100, "maxCredits": 100},
        {"usedCredits": 95, "maxCredits": 3000},
        {"usedCredits": 0, "maxCredits": 0},
        {},
        {"usedCredits": "90", "maxCredits": 100},
        {"usedCredits": -1, "maxCredits": 100},
        {"usedCredits": float("nan"), "maxCredits": 100},
    ],
)
def test_guard_blocks_before_transcript_request(monkeypatch, cache, data):
    get = Mock(return_value=response(data))
    monkeypatch.setattr(tracker.requests, "get", get)
    with pytest.raises(RuntimeError):
        tracker.get_transcript_text("key", "abcdefghijk")
    get.assert_called_once()
    assert get.call_args.args[0] == tracker.SUPADATA_ACCOUNT_URL


def test_guard_allows_last_budgeted_credit_and_renewed_allowance(monkeypatch):
    get = Mock(
        side_effect=[
            response({"usedCredits": 94, "maxCredits": 100}),
            response({"usedCredits": 0, "maxCredits": 100}),
        ]
    )
    monkeypatch.setattr(tracker.requests, "get", get)
    tracker.check_credit_budget("key")
    tracker.check_credit_budget("key")


def test_guard_fails_closed_when_account_api_down(monkeypatch, cache):
    get = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(tracker.requests, "get", get)
    with pytest.raises(requests.Timeout):
        tracker.get_transcript_text("key", "abcdefghijk")
    get.assert_called_once()


@pytest.mark.parametrize(
    "info,expected,cached",
    [
        ({"live_status": "is_live"}, False, False),
        ({"live_status": "is_upcoming"}, False, False),
        ({"live_status": "post_live"}, False, False),
        ({"live_status": "was_live", "duration": 3600}, True, True),
        ({"live_status": "not_live", "duration": 180}, False, True),
        ({"live_status": "not_live", "duration": 600}, True, True),
    ],
)
def test_video_filter_and_metadata_cache(monkeypatch, cache, info, expected, cached):
    youtube = Mock()
    youtube.extract_info.return_value = info
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=youtube)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tracker, "YoutubeDL", factory)
    assert tracker.video_is_ready("abcdefghijk") is expected
    assert tracker.cache_path("metadata", "abcdefghijk").exists() is cached
    if cached:
        assert tracker.video_is_ready("abcdefghijk") is expected
        youtube.extract_info.assert_called_once()


def test_empty_transcript_not_cached(monkeypatch, cache):
    transcript = response({"content": []})
    transcript.status_code = 200
    monkeypatch.setattr(
        tracker.requests,
        "get",
        Mock(side_effect=[response({"usedCredits": 90, "maxCredits": 100}), transcript]),
    )
    with pytest.raises(RuntimeError, match="empty"):
        tracker.get_transcript_text("key", "abcdefghijk")
    assert not tracker.cache_path("transcripts", "abcdefghijk").exists()
