"""Story schemas, local inference boundaries and bounded cancellable output."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from pydantic import ValidationError

from clipforge.common import Settings
from clipforge.job_control import JobCancelled, job_control
from clipforge.story_engine import (Ideas, Scene, Storyboard, StoryError, StoryOptions,
                                    _stream_parts, _structured, generate_ideas, rewrite_scene, write_story)


def board_fixture():
    return {"title": "The Other Window", "characters": [
        {"id": "alex", "name": "Alex", "color": "#c7b2f3", "accessory": "scarf"}],
        "scenes": [{"id": "invented-model-id", "narration": "My window showed tomorrow.",
                    "setting": "bedroom", "action": "look", "character_ids": ["alex"],
                    "camera": "zoom", "lighting": "normal", "effect": "none"},
                   {"id": "invented-model-id", "narration": "Then I saw myself turn around.",
                    "setting": "hallway", "action": "walk", "character_ids": ["alex"],
                    "camera": "pan", "lighting": "normal", "effect": "none"}]}


class StreamResponse:
    """Enough of Requests' streaming interface for either bounded reader style."""
    def __init__(self, content=None, *, lines=None, status=200, before_line=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/x-ndjson"}
        self.closed = False
        self.before_line = before_line
        if lines is None:
            text = json.dumps(content) if not isinstance(content, str) else content
            lines = [json.dumps({"message": {"content": text[i:i + 73]}}).encode()
                     for i in range(0, len(text), 73)]
            lines.append(b'{"done": true}')
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def iter_lines(self, chunk_size=256):
        for i, line in enumerate(self.lines):
            if self.before_line:
                self.before_line(i)
            yield line

    def iter_content(self, chunk_size=256):
        for i, line in enumerate(self.lines):
            if self.before_line:
                self.before_line(i)
            data = line + b"\n"
            for start in range(0, len(data), chunk_size):
                yield data[start:start + chunk_size]


def local_stub(*responses):
    http = Mock()
    http.post.side_effect = list(responses)
    return SimpleNamespace(base="http://127.0.0.1:11434", digest="sha256:installed-local", http=http)


def test_storyboard_normalizes_scene_ids_and_retains_one_character_library():
    raw = board_fixture()
    parsed = Storyboard.model_validate(raw)
    assert [s.id for s in parsed.scenes] == ["scene_01", "scene_02"]
    assert [c.id for c in parsed.characters] == ["alex"]
    assert all(s.character_ids == ["alex"] for s in parsed.scenes)
    assert raw["scenes"][0]["id"] == "invented-model-id"


@pytest.mark.parametrize("mutation", ["unknown_character", "duplicate_character", "too_many_words", "unexpected_field"])
def test_invalid_storyboard_cannot_reach_renderer(mutation):
    data = board_fixture()
    if mutation == "unknown_character":
        data["scenes"][0]["character_ids"] = ["new_character"]
    elif mutation == "duplicate_character":
        data["characters"].append(copy.deepcopy(data["characters"][0]))
    elif mutation == "too_many_words":
        data["scenes"] = [dict(data["scenes"][0], narration="word " * 200) for _ in range(4)]
    else:
        data["remote_image_url"] = "https://external.invalid/asset"
    with pytest.raises(ValidationError):
        Storyboard.model_validate(data)


@pytest.mark.parametrize("updates", [
    {"duration": 31}, {"duration": "60"}, {"quality": 2160}, {"quality": "720"},
    {"captions": "false"}, {"music": 1}, {"rate": True}, {"rate": 4},
    {"genre": "unsupported"}, {"idea": "bad\x00idea"}, {"voice": "bad\x01voice"},
    {"idea": "x" * 3001}, {"paid_api_key": "must-not-be-accepted"}])
def test_invalid_options_are_rejected(updates):
    with pytest.raises(ValidationError):
        StoryOptions.model_validate(updates)


def test_valid_local_stream_is_parsed_schema_bound_and_session_closed():
    response = StreamResponse(board_fixture())
    llm = local_stub(response)
    settings = Settings()
    with patch("clipforge.story_engine.LocalLLM", return_value=llm) as verified:
        board, provenance = write_story(settings, {"idea": "A window shows tomorrow", "duration": 60})
    verified.assert_called_once_with(settings)
    assert board["title"] == "The Other Window"
    assert provenance == {"model": settings.brain.model, "digest": "sha256:installed-local"}
    call = llm.http.post.call_args
    assert call.args[0] == llm.base + "/api/chat"
    assert call.kwargs["stream"] is True and call.kwargs["allow_redirects"] is False
    assert call.kwargs["json"]["keep_alive"] == 0
    assert call.kwargs["json"]["format"]["properties"]["characters"]
    instructions = json.loads(call.kwargs["json"]["messages"][1]["content"])
    assert instructions["target_seconds"] == 60 and instructions["scene_count"] == 6
    assert response.closed
    llm.http.close.assert_called_once()


def test_one_correction_revalidates_and_does_not_append_invalid_generation():
    first = StreamResponse("invalid generated JSON")
    second = StreamResponse(board_fixture())
    llm = local_stub(first, second)
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        board, _ = _structured(Settings(), "system", {"idea": "window"}, Storyboard)
    assert board.title == "The Other Window"
    assert llm.http.post.call_count == 2
    assert len(llm.http.post.call_args.kwargs["json"]["messages"]) == 3
    assert all("invalid generated JSON" not in m["content"]
               for m in llm.http.post.call_args.kwargs["json"]["messages"])
    assert first.closed and second.closed


def test_two_invalid_generations_fail_with_a_useful_error_and_stop_retrying():
    llm = local_stub(StreamResponse("not JSON"), StreamResponse("still not JSON"))
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        with pytest.raises(StoryError, match="valid storyboard"):
            _structured(Settings(), "system", {}, Storyboard)
    assert llm.http.post.call_count == 2
    llm.http.close.assert_called_once()


def test_generation_has_a_bounded_output_and_releases_stream():
    response = StreamResponse(lines=[json.dumps({"message": {"content": "x" * 60001}}).encode()])
    llm = local_stub(response)
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        with pytest.raises(StoryError):
            _structured(Settings(), "system", {}, Storyboard)
    assert response.closed
    llm.http.close.assert_called_once()


def test_stream_reader_bounds_unfinished_records_and_empty_heartbeat_traffic():
    response = SimpleNamespace(iter_content=lambda **kwargs: iter([b" " * 4096] * 17))
    with patch("clipforge.story_engine.time.monotonic", return_value=0):
        with pytest.raises(StoryError, match="oversized"):
            list(_stream_parts(response, 100))
    # Even traffic without completion text must have a finite transfer budget.
    response = SimpleNamespace(iter_content=lambda **kwargs: iter([b" " * 4095 + b"\n"] * 257))
    with patch("clipforge.story_engine.time.monotonic", return_value=0):
        with pytest.raises(StoryError, match="oversized"):
            list(_stream_parts(response, 100))


def test_stream_reader_deadline_stops_a_live_but_stalled_completion():
    response = SimpleNamespace(iter_content=lambda **kwargs: iter([b'{"done":false}\n']))
    with patch("clipforge.story_engine.time.monotonic", return_value=101):
        with pytest.raises(StoryError, match="too long"):
            list(_stream_parts(response, 100))


def test_stream_accepts_valid_last_record_without_newline():
    response = SimpleNamespace(iter_content=lambda **kwargs: iter([b'{"message":{"content":"finished"}}']))
    with patch("clipforge.story_engine.time.monotonic", return_value=0):
        assert list(_stream_parts(response, 100)) == ["finished"]


@pytest.mark.parametrize("line", [b"not valid transport json", b"[]", b'null',
                                   b'{"message":null}', b'{"message":{"content":42}}'])
def test_malformed_stream_returns_safe_story_error(line):
    response = StreamResponse(lines=[line])
    llm = local_stub(response)
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        with pytest.raises(StoryError):
            _structured(Settings(), "system", {}, Storyboard)
    assert response.closed
    llm.http.close.assert_called_once()


def test_cancellation_is_checked_while_streaming_not_only_after_full_response():
    cancelled = False
    def on_line(index):
        nonlocal cancelled
        if index == 1:
            cancelled = True
    response = StreamResponse(board_fixture(), before_line=on_line)
    llm = local_stub(response)
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        with pytest.raises(JobCancelled), job_control(cancelled=lambda: cancelled):
            _structured(Settings(), "system", {}, Storyboard)
    assert response.closed
    assert llm.http.post.call_count == 1
    llm.http.close.assert_called_once()


def test_unavailable_model_and_transport_errors_are_safe():
    llm = local_stub()
    llm.http.post.side_effect = requests.ReadTimeout("contains private transport context")
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        with pytest.raises(StoryError, match="unavailable or timed out") as error:
            _structured(Settings(), "system", {}, Storyboard)
    assert "private transport" not in str(error.value)
    llm.http.close.assert_called_once()


@pytest.mark.parametrize("unsafe", [
    {"remote_host": "https://hosted.invalid"}, {"remote_model": "cloud"},
    {"details": {"format": "remote"}}])
def test_actual_local_llm_checks_reject_remote_alias_before_any_story_call(unsafe):
    show = {"details": {"format": "gguf"}, "model_info": {"parameter_count": 4_000_000_000}}
    show.update(unsafe)
    response = Mock(status_code=200)
    response.json.return_value = show
    http = Mock(); http.post.return_value = response
    with patch("clipforge.brain.requests.Session", return_value=http):
        with pytest.raises(StoryError, match="unavailable or timed out"):
            _structured(Settings(), "system", {}, Storyboard)
    assert http.post.call_count == 1
    assert http.post.call_args.args[0].endswith("/api/show")
    assert http.post.call_args.kwargs["allow_redirects"] is False
    assert http.trust_env is False
    http.get.assert_not_called()


def test_scene_rewrite_retains_selected_id_and_rejects_new_characters():
    project = Storyboard.model_validate(board_fixture()).model_dump()
    replacement = dict(board_fixture()["scenes"][0], id="new-id", narration="Tomorrow, the other boy waved.")
    llm = local_stub(StreamResponse(replacement))
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        result, _ = rewrite_scene(Settings(), project, "scene_01")
    assert result["id"] == "scene_01"
    assert result["narration"] == replacement["narration"]
    replacement["character_ids"] = ["stranger"]
    llm = local_stub(StreamResponse(replacement))
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        with pytest.raises(StoryError, match="character library"):
            rewrite_scene(Settings(), project, "scene_01")


def test_missing_scene_fails_before_using_local_model():
    with patch("clipforge.story_engine.LocalLLM") as local:
        with pytest.raises(StoryError, match="no longer"):
            rewrite_scene(Settings(), Storyboard.model_validate(board_fixture()).model_dump(), "missing")
    local.assert_not_called()


def test_idea_generation_needs_five_different_complete_premises():
    five = {"ideas": [f"A different world appears in window number {i}." for i in range(5)]}
    llm = local_stub(StreamResponse(five))
    with patch("clipforge.story_engine.LocalLLM", return_value=llm):
        assert generate_ideas(Settings()) == five
    for bad in ({"ideas": ["one"] * 5}, {"ideas": five["ideas"][:4]}, {"ideas": [five["ideas"][0]] * 5}):
        with pytest.raises(ValidationError):
            Ideas.model_validate(bad)
