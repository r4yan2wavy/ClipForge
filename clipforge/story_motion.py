"""Pure, deterministic choreography shared by every frame of a story.

The model supplies bounded acting cues. This module compiles those cues onto
absolute tracks, retaining a character's position, walking phase, held prop and
pose across narration scenes. Old projects receive the same deterministic
fallback choreography whenever they are reopened.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass

MOTION_VERSION = "2"
PORTABLE_PROPS = ("phone", "key", "book", "flashlight")
EXPRESSIONS = {
    "neutral": (0., 0., 1.), "curious": (-.25, .05, 1.10),
    "worried": (-.65, .12, .9), "surprised": (-.9, .8, 1.35),
    "determined": (.65, -.1, .85),
}


def smooth(value):
    value = max(0., min(1., value))
    return value * value * (3 - 2 * value)


def _blend(first, last, fraction):
    p = smooth(fraction)
    result = {}
    for key in first.keys() | last.keys():
        a, b = first.get(key, last.get(key)), last.get(key, first.get(key))
        result[key] = a + (b - a) * p if isinstance(a, (int, float)) and isinstance(b, (int, float)) else (a if p < .5 else b)
    return result


@dataclass
class Track:
    keys: list

    def __post_init__(self):
        self.keys.sort(key=lambda key: key[0])
        self.times = [key[0] for key in self.keys]

    def at(self, seconds):
        index = bisect.bisect_right(self.times, seconds) - 1
        if index < 0:
            return self.keys[0][1].copy()
        if index >= len(self.keys) - 1:
            return self.keys[-1][1].copy()
        start, first = self.keys[index]
        end, last = self.keys[index + 1]
        return _blend(first, last, (seconds - start) / max(.0001, end - start))


def _key(keys, when, state):
    when = round(float(when), 7)
    if keys and abs(keys[-1][0] - when) < .000001:
        keys[-1] = (when, state.copy())
    else:
        keys.append((when, state.copy()))


def inferred_prop(scene):
    prop = scene.get("prop", "none")
    if prop in PORTABLE_PROPS:
        return prop
    text = str(scene.get("narration", "")).lower()
    for name in PORTABLE_PROPS:
        if re.search(r"\b" + name + r"s?\b", text):
            return name
    return "none"


def fallback_beats(scene):
    """Meaningful connected cues for existing projects without acting beats."""
    ids = scene.get("character_ids", [])
    if not ids:
        return []
    hero = ids[0]
    text = str(scene.get("narration", "")).lower()
    action = scene.get("action", "look")
    feature = "window" if "window" in text else "door" if any(t in text for t in ("door", "knock", "hall")) else "none"
    expression = "worried" if any(t in text for t in ("shadow", "behind", "dark", "missing", "afraid", "alone")) else "curious"
    prop = inferred_prop(scene)
    if action == "exit":
        cues = [(0., "look", "partner"), (.24, "turn", "right"), (.48, "exit", "right")]
    elif feature != "none":
        cues = [(0., "walk", feature), (.31, "turn", feature), (.49, "reach", feature)]
        cues.append((.67, "open" if any(t in text for t in ("open", "unlock", "push")) else "close" if "clos" in text else "look", feature))
        cues.append((.86, "react", feature))
    elif prop != "none":
        cues = [(0., "look", "prop"), (.25, "reach", "prop"), (.43, "pick_up", "prop"), (.73, "hold", "prop")]
    elif action in {"run", "walk", "enter"}:
        cues = [(0., "look", "right"), (.18, action, "right"), (.65, "turn", "left"), (.83, "react", "partner")]
    elif action in {"open", "close", "reach", "pick_up", "hold", "crouch", "react", "point", "hide"}:
        cues = [(0., "look", "partner"), (.25, action, "prop" if prop != "none" else "right"), (.67, "turn", "left"), (.84, "look", "partner")]
    else:
        cues = [(0., "look", "right"), (.28, "turn", "left"), (.55, "point" if "saw" in text else "look", "partner"), (.82, "react", "partner")]
    result = [{"at": at, "character_id": hero, "action": act, "target": target,
               "expression": "surprised" if act == "react" else expression} for at, act, target in cues]
    for other in ids[1:]:
        result.append({"at": .15, "character_id": other, "action": "look", "target": "partner", "expression": "neutral"})
    return result[:8]


def _base_state(index):
    positions = [(395., 1090., 1.12), (206., 1010., .87), (591., 1080., 1.), (90., 1095., .95)]
    x, y, scale = positions[min(index, 3)]
    return {"x": x, "y": y, "scale": scale, "facing": 1. if index == 0 else -1.,
            "phase": 0., "walk": 0., "reach": 0., "point": 0., "guard": 0., "crouch": 0.,
            "reaction": 0., "lean": 0., "gaze": .5, "brow": 0., "mouth": 0., "eyes": 1.,
            "visibility": 1., "holding": 0., "prop_kind": "none", "target_x": x + 80, "target_y": y - 180}


def _target(state, cue, scene, partner_x):
    target = cue.get("target", "none")
    locations = {"left": (160., state["y"]), "center": (365., state["y"]), "right": (570., state["y"]),
                 "window": (566., 1035.), "door": (302., 1070.), "prop": (430., 1080.),
                 "partner": (max(90., min(630., partner_x + (90 if partner_x < state["x"] else -90))), state["y"])}
    if target == "none":
        destination = (max(90., min(630., state["x"] + (70 if state["facing"] >= 0 else -70))), state["y"])
    else:
        destination = locations[target]
    hands = {"window": (630., 748.), "door": (270., 798.), "prop": (430., 1038.)}
    look = hands.get(target, (partner_x, state["y"] - 210) if target == "partner" else (destination[0], state["y"] - 190))
    return destination, look


class MotionPlan:
    """Compiled tracks can be sampled in any order, including preview seeks."""
    def __init__(self, project):
        self.characters = {c["id"]: c for c in project.get("characters", [])}
        self.scenes = project.get("scenes", [])
        self.tracks = {}
        self.objects = {}
        object_keys, object_values = {}, {}
        camera_keys, camera = [], {"zoom": 1.025, "pan_x": 0., "pan_y": 0.}
        _key(camera_keys, 0, camera)
        states = {cid: _base_state(index) for index, cid in enumerate(self.characters)}
        raw_keys = {cid: [] for cid in self.characters}
        first_ids = self.scenes[0].get("character_ids", []) if self.scenes else []
        for cid, state in states.items():
            state["visibility"] = 1. if cid in first_ids else 0.
            _key(raw_keys[cid], 0, state)
        self.beats = []
        for scene_index, scene in enumerate(self.scenes):
            start, duration = float(scene.get("start_s", 0)), float(scene.get("duration_s", 1))
            end = start + duration
            ids = scene.get("character_ids", [])
            cues = scene.get("beats") or fallback_beats(scene)
            self.beats.append(cues)
            prop = inferred_prop(scene)
            for cid, state in states.items():
                keys = raw_keys[cid]
                _key(keys, start, state)
                if cid not in ids:
                    state = dict(state, visibility=0., walk=0.)
                    _key(keys, min(end, start + .45), state)
                    _key(keys, end, state)
                    states[cid] = state
                    continue
                cues_for_character = sorted([b for b in cues if b.get("character_id") == cid], key=lambda b: b["at"])
                if not cues_for_character:
                    cues_for_character = [{"at": .05, "action": "look", "target": "partner", "expression": "neutral"}]
                first_time = start + max(0., min(.99, cues_for_character[0]["at"])) * duration
                if first_time > start:
                    state = dict(state, visibility=1.)
                    _key(keys, min(first_time, start + .35), state)
                    _key(keys, first_time, state)
                for index, cue in enumerate(cues_for_character):
                    begin = start + max(0., min(.99, cue["at"])) * duration
                    finish = start + max(0., min(1., cues_for_character[index + 1]["at"])) * duration if index + 1 < len(cues_for_character) else end
                    span = max(.01, finish - begin)
                    _key(keys, begin, state)
                    partner = next((states[k]["x"] for k in ids if k != cid and k in states), 360.)
                    destination, hand_target = _target(state, cue, scene, partner)
                    action = cue.get("action", "look")
                    moving = action in {"walk", "run", "enter", "exit"}
                    goal = dict(state, visibility=1., walk=1. if moving else 0., reach=0., point=0.,
                                guard=0., crouch=0., reaction=0., lean=0., target_x=hand_target[0], target_y=hand_target[1])
                    brow, mouth, eyes = EXPRESSIONS.get(cue.get("expression", "neutral"), EXPRESSIONS["neutral"])
                    goal.update(brow=brow, mouth=mouth, eyes=eyes)
                    if abs(hand_target[0] - state["x"]) > 4:
                        goal["facing"] = 1. if hand_target[0] > state["x"] else -1.
                    if action == "turn" and cue.get("target", "none") == "none":
                        goal["facing"] = -1. if state["facing"] >= 0 else 1.
                    if moving:
                        goal.update(x=destination[0], y=destination[1])
                        if action == "exit":
                            goal["x"] = 805. if goal["facing"] >= 0 else -85.
                        if action == "enter" and (state["x"] < 20 or state["x"] > 700):
                            goal["x"] = 340.
                        goal["phase"] = state["phase"] + abs(goal["x"] - state["x"]) / (24. if action == "run" else 33.)
                    elif action in {"reach", "open", "close"}:
                        goal["reach"] = 1.
                    elif action == "point":
                        goal["point"] = 1.
                    elif action == "hide":
                        goal.update(guard=1., crouch=.25)
                    elif action == "crouch":
                        goal["crouch"] = .8
                    elif action == "react":
                        goal.update(reaction=1., lean=-.25, mouth=max(.5, mouth), eyes=max(1.2, eyes))
                    elif action == "pick_up":
                        goal.update(crouch=.65, reach=.9, prop_kind=prop if prop != "none" else "book")
                    elif action == "hold":
                        goal.update(holding=1., prop_kind=prop if prop != "none" else (state["prop_kind"] if state["prop_kind"] != "none" else "book"))
                    # Pose change and locomotion are separate keys: settle into
                    # the action, complete its travel, then ease to the next cue.
                    anticipation = min(.28, span * .22)
                    early = goal.copy()
                    if moving:
                        early.update(x=state["x"] + (goal["x"] - state["x"]) * .08,
                                     y=state["y"] + (goal["y"] - state["y"]) * .08,
                                     phase=state["phase"] + (goal["phase"] - state["phase"]) * .08)
                    _key(keys, begin + anticipation, early)
                    _key(keys, begin + span * .78, goal)
                    final = goal.copy()
                    if moving:
                        final["walk"] = 0.
                        if action == "exit":
                            final["visibility"] = 0.
                    if action == "pick_up":
                        final.update(crouch=0., reach=0., holding=1.)
                    if action == "react":
                        final.update(reaction=.15, lean=0.)
                    if action in {"open", "close"}:
                        target = cue.get("target", "none")
                        if target not in {"door", "window"}:
                            target = "door" if "door" in str(scene.get("narration", "")).lower() else "window"
                        oid = f"{scene.get('setting', 'bedroom')}:{target}"
                        previous = object_values.get(oid, 0.)
                        target_open = 1. if action == "open" else 0.
                        obj = object_keys.setdefault(oid, [(0., {"open": previous})])
                        _key(obj, begin, {"open": previous})
                        _key(obj, begin + span * .78, {"open": target_open})
                        object_values[oid] = target_open
                        final["reach"] = .2
                    _key(keys, finish, final)
                    state = final
                _key(keys, end, state)
                states[cid] = state
            _key(camera_keys, start, camera)
            camera_goal = camera.copy()
            kind = scene.get("camera", "zoom")
            if kind == "zoom":
                camera_goal["zoom"] = 1.075 if scene_index % 2 == 0 else 1.035
            elif kind == "pan":
                camera_goal.update(pan_x=14. if scene_index % 2 == 0 else -14., zoom=1.045)
            _key(camera_keys, end, camera_goal)
            camera = camera_goal
        self.tracks = {cid: Track(keys) for cid, keys in raw_keys.items()}
        self.objects = {oid: Track(keys) for oid, keys in object_keys.items()}
        self.camera = Track(camera_keys)

    def character(self, cid, seconds):
        return self.tracks[cid].at(seconds)

    def object(self, setting, kind, seconds):
        track = self.objects.get(f"{setting}:{kind}")
        return track.at(seconds)["open"] if track else 0.

    def view(self, seconds):
        return self.camera.at(seconds)
