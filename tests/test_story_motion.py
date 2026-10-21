"""Continuity, deterministic seeking and bounded authored acting cues."""
import copy
import math

import pytest
from pydantic import ValidationError

from clipforge.story_animation import FrameRenderer, available
from clipforge.story_engine import Storyboard
from clipforge.story_motion import MotionPlan, fallback_beats
from clipforge.story_service import StoryServiceMixin


def project():
    value = Storyboard.model_validate({
        "title": "The Quiet Door",
        "characters": [{"id": "hero", "name": "Ari", "color": "#ffc16b", "accessory": "scarf"}],
        "scenes": [
            {"narration": "Ari walked toward the locked bedroom door with a key.", "setting": "bedroom",
             "action": "walk", "character_ids": ["hero"], "camera": "pan", "prop": "key",
             "beats": [{"at": 0, "character_id": "hero", "action": "pick_up", "target": "prop"},
                       {"at": .25, "character_id": "hero", "action": "walk", "target": "door"},
                       {"at": .65, "character_id": "hero", "action": "open", "target": "door"}]},
            {"narration": "She turned to face the window and kept the key in her hand.", "setting": "bedroom",
             "action": "turn", "character_ids": ["hero"], "camera": "zoom", "prop": "key",
             "beats": [{"at": 0, "character_id": "hero", "action": "turn", "target": "window"},
                       {"at": .45, "character_id": "hero", "action": "walk", "target": "window"},
                       {"at": .8, "character_id": "hero", "action": "react", "expression": "surprised"}]}
        ]
    }).model_dump()
    value["options"] = {"quality": 720, "captions": False}
    value["duration_s"] = 12.
    for index, scene in enumerate(value["scenes"]):
        scene.update(start_s=index * 6., duration_s=6.)
    return value


def test_position_pose_phase_camera_and_held_prop_do_not_reset_at_narration_boundary():
    plan = MotionPlan(project())
    before, after = plan.character("hero", 6 - .00001), plan.character("hero", 6 + .00001)
    for name in ("x", "y", "scale", "facing", "phase", "walk", "reach", "holding"):
        assert abs(before[name] - after[name]) < .001, name
    assert after["holding"] == 1
    assert after["prop_kind"] == "key"
    assert before["x"] != 395, "Position must reflect the prior walk, not a scene default"
    assert plan.object("bedroom", "door", 6 - .001) > .99
    assert plan.object("bedroom", "door", 10) > .99
    for name in ("zoom", "pan_x", "pan_y"):
        assert abs(plan.view(6 - .00001)[name] - plan.view(6 + .00001)[name]) < .001


@pytest.mark.skipif(not available(), reason="Install optional Pillow story support")
def test_rig_and_world_frames_stay_continuous_without_black_scene_gaps():
    from PIL import ImageChops, ImageStat
    value = project()
    renderer = FrameRenderer(value)
    before, after = renderer.frame(5.999), renderer.frame(6.001)
    difference = sum(ImageStat.Stat(ImageChops.difference(before, after)).mean) / 3
    assert difference < 1.0
    value["scenes"][1]["setting"] = "forest"
    renderer = FrameRenderer(value)
    for seconds in (0, 5.999, 6, 6.25, 6.75, 11.99):
        assert sum(ImageStat.Stat(renderer.frame(seconds)).mean) / 3 > 10


@pytest.mark.skipif(not available(), reason="Install optional Pillow story support")
def test_seek_order_is_deterministic_and_does_not_mutate_saved_project():
    value = project()
    saved = copy.deepcopy(value)
    renderer = FrameRenderer(value)
    late = renderer.frame(9.5).tobytes()
    renderer.frame(.2)
    renderer.frame(6.1)
    assert renderer.frame(9.5).tobytes() == late
    assert value == saved


def test_legacy_projects_receive_multiple_meaningful_beats_and_stable_tracks():
    value = project()
    for scene in value["scenes"]:
        scene.pop("beats")
    cues = fallback_beats(value["scenes"][0])
    assert len(cues) >= 3
    assert len({cue["action"] for cue in cues}) >= 3
    plan = MotionPlan(value)
    assert all(math.isfinite(v) for v in plan.character("hero", 4).values() if isinstance(v, float))
    assert plan.character("hero", 6)["x"] == pytest.approx(plan.character("hero", 5.999999)["x"], abs=.001)


def test_schema_rejects_foreign_characters_duplicate_beats_and_excessive_cues():
    base = project()
    for kind in ("foreign", "duplicate", "overflow", "badtime", "unknownaction"):
        value = copy.deepcopy(base)
        value.pop("options"); value.pop("duration_s")
        for scene in value["scenes"]:
            scene.pop("start_s"); scene.pop("duration_s")
        beats = value["scenes"][0]["beats"]
        if kind == "foreign":
            beats[0]["character_id"] = "outsider"
        elif kind == "duplicate":
            beats.append(copy.deepcopy(beats[0]))
        elif kind == "overflow":
            value["scenes"][0]["beats"] = [dict(beats[0], at=i / 10) for i in range(9)]
        elif kind == "badtime":
            beats[0]["at"] = float("nan")
        else:
            beats[0]["action"] = "execute_python"
        with pytest.raises(ValidationError):
            Storyboard.model_validate(value)


def test_downloaded_srt_keeps_short_measured_phrase_cues():
    scenes = [{"words": [{"text": "The door opened.", "start_s": 0, "end_s": 2, "timing": "phrase"},
                         {"text": "Someone was waiting.", "start_s": 2, "end_s": 4, "timing": "phrase"}]}]
    srt = StoryServiceMixin._story_srt(scenes)
    assert "00:00:00,000 --> 00:00:02,000\nThe door opened." in srt
    assert "00:00:02,000 --> 00:00:04,000\nSomeone was waiting." in srt
