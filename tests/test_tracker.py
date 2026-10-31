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


def youtube_factory(entries):
    youtube = Mock()
    youtube.extract_info.return_value = {"entries": entries}
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=youtube)
    factory.return_value.__exit__ = Mock(return_value=False)
    return factory, youtube


def test_uploads_playlist_skips_shorts_and_unfinished_streams(monkeypatch):
    factory, youtube = youtube_factory(
        [
            {"id": "short111111", "duration": 45},
            {"id": "clip1111111", "duration": 120},
            {"id": "edge1111111", "duration": 180},
            {"id": "live1111111", "duration": 4000, "live_status": "is_live"},
            {"id": "soon1111111", "duration": 4000, "live_status": "post_live"},
            {"id": "newestlong1", "duration": 5421, "live_status": "was_live"},
            {"id": "olderlong11", "duration": 181},
        ]
    )
    monkeypatch.setattr(tracker, "YoutubeDL", factory)

    def fail_request(*args, **kwargs):
        raise AssertionError(f"discovery must not call HTTP APIs: {args} {kwargs}")

    monkeypatch.setattr(tracker.requests, "get", fail_request)
    assert tracker.get_latest_longform_video_id(tracker.CHANNEL_ID) == "newestlong1"
    options = factory.call_args.args[0]
    assert options["extract_flat"] is True
    assert options["playlistend"] == tracker.UPLOADS_LOOKBACK
    url = youtube.extract_info.call_args.args[0]
    assert url.endswith("list=UUS01CiRDAiyhR_mTHXDW23A")
    assert youtube.extract_info.call_args.kwargs["download"] is False


def test_discovery_errors_are_not_reported_as_no_new_video(monkeypatch):
    factory = Mock()
    factory.return_value.__enter__ = Mock(side_effect=RuntimeError("listing blocked"))
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tracker, "YoutubeDL", factory)
    with pytest.raises(RuntimeError, match="listing blocked"):
        tracker.get_latest_longform_video_id(tracker.CHANNEL_ID)


def test_missing_uploads_listing_is_an_error(monkeypatch):
    factory, _youtube = youtube_factory(None)
    factory.return_value.__enter__.return_value.extract_info.return_value = {}
    monkeypatch.setattr(tracker, "YoutubeDL", factory)
    with pytest.raises(RuntimeError, match="uploads"):
        tracker.get_latest_longform_video_id(tracker.CHANNEL_ID)


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    for key in ("GEMINI_API_KEY", "SUPADATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "test")
    monkeypatch.delenv("FORCE_RUN", raising=False)
    state = tmp_path / "last_video.txt"
    state.write_text("bloom")
    monkeypatch.setattr(tracker, "LAST_VIDEO_PATH", str(state))
    monkeypatch.setattr(tracker, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(tracker, "get_latest_longform_video_id", Mock(return_value="teslavideo1"))
    monkeypatch.setattr(tracker, "get_transcript_text", Mock(return_value="Transcript"))
    monkeypatch.setattr(tracker, "summarize_transcript", Mock(return_value="Summary"))
    monkeypatch.setattr(tracker, "telegram_send", Mock())
    return state


def test_new_replay_sent_once_and_checkpointed(pipeline):
    tracker.main()
    assert pipeline.read_text() == "teslavideo1"
    assert "watch?v=teslavideo1" in tracker.telegram_send.call_args.args[2]
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


def test_flat_listings_skip_shorts_and_unfinished_streams(monkeypatch):
    youtube = Mock()
    youtube.extract_info.side_effect = [
        {"entries": [{"id": "upload", "duration": 400}, {"id": "clip", "duration": 180}]},
        {
            "entries": [
                {"id": "replay", "duration": 4000, "live_status": "was_live"},
                {"id": "live", "duration": 4000, "live_status": "is_live"},
                {"id": "upcoming", "live_status": "is_upcoming"},
                {"id": "processing", "duration": 4000, "live_status": "post_live"},
            ]
        },
    ]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=youtube)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tracker, "YoutubeDL", factory)
    assert tracker.get_channel_video_status("channel") == {
        "upload": True,
        "clip": False,
        "replay": True,
        "live": False,
        "upcoming": False,
        "processing": False,
    }
    assert factory.call_args.args[0]["extract_flat"] is True
    assert [c.args[0].rsplit("/", 1)[-1] for c in youtube.extract_info.call_args_list] == [
        "videos",
        "streams",
    ]
    assert all(c.kwargs["download"] is False for c in youtube.extract_info.call_args_list)


def test_telegram_failure_reuses_summary_without_another_gemini_call(monkeypatch, tmp_path):
    for key in ("GEMINI_API_KEY", "SUPADATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "test")
    monkeypatch.delenv("FORCE_RUN", raising=False)
    state = tmp_path / "last_video.txt"
    state.write_text("bloom")
    monkeypatch.setattr(tracker, "LAST_VIDEO_PATH", str(state))
    monkeypatch.setattr(tracker, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(tracker, "get_latest_longform_video_id", Mock(return_value="abcdefghijk"))
    summarize = Mock(return_value="Summary")
    transcript = Mock(return_value="Transcript")
    monkeypatch.setattr(tracker, "summarize_transcript", summarize)
    monkeypatch.setattr(tracker, "get_transcript_text", transcript)
    monkeypatch.setattr(
        tracker, "telegram_send", Mock(side_effect=[RuntimeError("telegram down"), None])
    )
    with pytest.raises(RuntimeError, match="telegram"):
        tracker.main()
    assert state.read_text() == "bloom"
    tracker.main()
    assert state.read_text() == "abcdefghijk"
    summarize.assert_called_once()
    transcript.assert_called_once()
    assert tracker.telegram_send.call_count == 2


class _Response:
    def __init__(self, text):
        self.text = text


def _install_gemini(monkeypatch, generate):
    created = {}

    class FakeClient:
        def __init__(self, api_key, http_options=None):
            created["api_key"] = api_key
            created["http_options"] = http_options
            self.models = Mock()
            self.models.generate_content.side_effect = generate

    monkeypatch.setattr(tracker.genai, "Client", FakeClient)
    return created


def test_overloaded_model_falls_back_without_sdk_retries(monkeypatch):
    calls = []

    def generate(*, model, contents, config=None):
        calls.append(model)
        if model == tracker.MODEL_NAME:
            raise tracker.genai_errors.ServerError(
                503,
                {
                    "error": {
                        "code": 503,
                        "status": "UNAVAILABLE",
                        "message": "high demand",
                    }
                },
            )
        return _Response("Chris likes the energy trade")

    created = _install_gemini(monkeypatch, generate)
    assert (
        tracker.summarize_transcript("Dumb Money Live", "word " * 30, "secret-key")
        == "Chris likes the energy trade"
    )
    assert calls == [
        tracker.MODEL_NAME,
        tracker.FALLBACK_MODEL_NAMES[0],
        tracker.MODEL_NAME,
        tracker.FALLBACK_MODEL_NAMES[0],
    ]
    assert created["http_options"]["retry_options"]["attempts"] == 1
    assert created["http_options"]["timeout"] == tracker.GEMINI_TIMEOUT_MS
    assert created["api_key"] == "secret-key"


def test_dropped_connection_uses_fallback_model(monkeypatch):
    class RemoteProtocolError(Exception):
        pass

    calls = []

    def generate(*, model, contents, config=None):
        calls.append(model)
        if model == tracker.MODEL_NAME:
            raise RemoteProtocolError("Server disconnected without sending a response.")
        return _Response("fallback summary")

    _install_gemini(monkeypatch, generate)
    assert tracker.summarize_transcript("title", "transcript words", "key") == "fallback summary"
    assert calls == [
        tracker.MODEL_NAME,
        tracker.FALLBACK_MODEL_NAMES[0],
        tracker.MODEL_NAME,
        tracker.FALLBACK_MODEL_NAMES[0],
    ]


def test_rejected_thinking_config_is_retried_without_it(monkeypatch):
    seen = []

    def generate(*, model, contents, config=None):
        seen.append(config)
        if config and "thinking_config" in config:
            raise tracker.genai_errors.ClientError(
                400,
                {
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "thinking",
                    }
                },
            )
        return _Response("summary without thinking")

    _install_gemini(monkeypatch, generate)
    assert (
        tracker.summarize_transcript("title", "transcript words", "key")
        == "summary without thinking"
    )
    assert seen[0]["thinking_config"]["thinking_level"] == "minimal"
    assert "thinking_config" not in seen[1]
    assert all(
        config.get("max_output_tokens") == tracker.GEMINI_MAX_OUTPUT_TOKENS for config in seen
    )


def test_auth_errors_do_not_try_another_model(monkeypatch):
    calls = []

    def generate(*, model, contents, config=None):
        calls.append(model)
        raise tracker.genai_errors.ClientError(
            401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "bad key"}}
        )

    _install_gemini(monkeypatch, generate)
    with pytest.raises(tracker.genai_errors.ClientError):
        tracker.summarize_transcript("title", "transcript words", "key")
    assert calls == [tracker.MODEL_NAME]


def test_every_free_model_down_leaves_summary_pending(monkeypatch):
    calls = []

    def generate(*, model, contents, config=None):
        calls.append(model)
        raise tracker.genai_errors.ClientError(
            429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}
        )

    _install_gemini(monkeypatch, generate)
    with pytest.raises(tracker.genai_errors.ClientError):
        tracker.summarize_transcript("title", "transcript words", "key")
    assert calls == [tracker.MODEL_NAME, *tracker.FALLBACK_MODEL_NAMES]


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
