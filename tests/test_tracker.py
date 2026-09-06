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


@pytest.mark.parametrize(
    "upload_date,live_date,expected",
    [
        ("2026-07-14T00:00:00Z", "2026-09-05T00:00:00Z", "tesla"),
        ("2026-09-06T00:00:00Z", "2026-09-05T00:00:00Z", "upload"),
    ],
)
def test_discovery_compares_both_tabs(monkeypatch, upload_date, live_date, expected):
    get = Mock(
        side_effect=[
            response({"videoIds": ["upload"], "shortIds": ["short"]}),
            response({"liveIds": ["tesla"]}),
            response({"createdAt": upload_date}),
            response({"createdAt": live_date}),
        ]
    )
    monkeypatch.setattr(tracker.requests, "get", get)
    assert tracker.get_latest_longform_video_id("key", "channel") == expected
    assert [call.kwargs["params"]["type"] for call in get.call_args_list[:2]] == ["video", "live"]


def test_live_only_channel(monkeypatch):
    monkeypatch.setattr(
        tracker.requests,
        "get",
        Mock(
            side_effect=[
                response({"videoIds": [], "shortIds": ["short"]}),
                response({"liveIds": ["tesla"]}),
                response({"createdAt": "2026-09-05T00:00:00Z"}),
            ]
        ),
    )
    assert tracker.get_latest_longform_video_id("key", "channel") == "tesla"


def test_discovery_errors_are_not_reported_as_no_new_video(monkeypatch):
    failed = Mock()
    failed.raise_for_status.side_effect = requests.HTTPError("unavailable")
    monkeypatch.setattr(tracker.requests, "get", Mock(return_value=failed))
    with pytest.raises(requests.HTTPError):
        tracker.get_latest_longform_video_id("key", "channel")


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
