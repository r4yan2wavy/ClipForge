"""Deterministic local 2D animation for the story studio.

Characters are drawn from a persistent project library.  Narration controls the
timeline; no still-image generator or video diffusion service is required.
Pillow is imported only when this optional workflow is used.
"""
from __future__ import annotations

import bisect
import contextlib
import hashlib
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

from .job_control import checkpoint, report
from .story_motion import MotionPlan, inferred_prop, smooth

ANIMATION_VERSION = "2"
BASE_W, BASE_H = 720, 1280
SETTINGS = {"bedroom", "hallway", "street", "school", "forest", "rooftop"}
BACKDROPS = {"night", "forest", "city", "desert", "otherworld"}


def available() -> bool:
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError as error:
        raise ValueError("Install ClipForge's story support: python -m pip install -e '.[stories]'.") from error
    return Image, ImageDraw, ImageFont, ImageFilter


def _mix(a, b, x):
    return tuple(round(i + (j - i) * x) for i, j in zip(a, b))


def _smooth(x):
    x = max(0., min(1., x))
    return x * x * (3 - 2 * x)


def _seed(value):
    return int(hashlib.sha256(str(value).encode()).hexdigest()[:12], 16)


def _accent(character):
    value = character.get("color", "#c7b2f3")
    if isinstance(value, str) and len(value) == 7 and value[0] == "#":
        try:
            return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return 199, 178, 243


@lru_cache(maxsize=16)
def _font(size, bold=True):
    _, _, ImageFont, _ = _pil()
    # Installed operating-system fonts stay on the machine. No font download.
    choices = (["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
               if bold else ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for path in choices:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _background(scene):
    Image, ImageDraw, _, ImageFilter = _pil()
    vision = scene.get("lighting") == "nightvision"
    top, bottom = ((6, 18, 12), (17, 44, 30)) if vision else ((10, 9, 23), (33, 24, 52))
    image = Image.new("RGB", (BASE_W, BASE_H))
    draw = ImageDraw.Draw(image)
    for y in range(BASE_H):
        draw.line((0, y, BASE_W, y), fill=_mix(top, bottom, y / BASE_H))
    glow = Image.new("RGBA", image.size)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((210, 260, 760, 1000), fill=(78, 88, 140, 38) if not vision else (83, 169, 86, 38))
    image = Image.alpha_composite(image.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(75))).convert("RGB")
    d = ImageDraw.Draw(image)
    line, dark, wall = (68, 64, 91), (9, 10, 19), (27, 24, 43)
    setting = scene.get("setting", "bedroom")
    if setting not in SETTINGS:
        setting = "bedroom"
    if setting in {"street", "forest", "rooftop"}:
        for i in range(75):
            x, y = (i * 173 + 37) % 720, (i * 71 + 91) % 660 + 160
            d.ellipse((x, y, x + 1, y + 1), fill=(123, 117, 157))
        d.ellipse((480, 285, 562, 367), fill=(186, 188, 210))
        d.ellipse((455, 266, 535, 350), fill=(16, 14, 30))
        d.rectangle((0, 1000, 720, 1280), fill=(14, 13, 24))
        d.line((0, 1000, 720, 1000), fill=line, width=2)
    if setting == "bedroom":
        d.polygon([(0, 270), (100, 360), (100, 1010), (0, 1130)], fill=(20, 17, 33))
        d.rectangle((100, 360, 720, 1010), outline=line, width=3)
        d.polygon([(0, 1130), (100, 1010), (720, 1010), (720, 1280), (0, 1280)], fill=(21, 18, 32))
        for x in range(-250, 1000, 160):
            d.line((360, 1010, x, 1280), fill=(43, 35, 57), width=2)
        # A window becomes the consistent visual focal point in mystery stories.
        d.rectangle((440, 445, 664, 775), fill=(7, 13, 30), outline=(109, 111, 149), width=5)
        _window_world(d, scene.get("backdrop", "night"))
        d.line((552, 445, 552, 775), fill=(88, 86, 116), width=5)
        d.line((440, 610, 664, 610), fill=(88, 86, 116), width=5)
        d.rectangle((422, 438, 439, 805), fill=(47, 27, 61))
        d.rectangle((666, 438, 690, 805), fill=(47, 27, 61))
        d.polygon([(455, 775), (647, 775), (720, 1140), (282, 1140)], fill=(44, 43, 69))
        d.rectangle((25, 940, 300, 1075), fill=(32, 27, 43), outline=line, width=3)
        d.polygon([(25, 940), (90, 900), (330, 900), (300, 940)], fill=(72, 66, 93))
        d.rectangle((30, 955, 290, 1000), fill=(64, 55, 82))
        d.rectangle((50, 905, 127, 935), fill=(116, 107, 138))
        d.rectangle((140, 405, 290, 1010), fill=dark, outline=(68, 62, 88), width=4)
    elif setting in {"hallway", "school"}:
        d.polygon([(0, 340), (285, 575), (285, 945), (0, 1200)], fill=(27, 23, 43))
        d.polygon([(720, 340), (435, 575), (435, 945), (720, 1200)], fill=(21, 20, 36))
        d.polygon([(0, 1200), (285, 945), (435, 945), (720, 1200), (720, 1280), (0, 1280)], fill=(19, 18, 31))
        d.rectangle((285, 575, 435, 945), fill=dark, outline=(57, 49, 78), width=3)
        for y in (983, 1048, 1143, 1268):
            d.line((0, y, 720, y), fill=(50, 43, 64), width=2)
        for x in (60, 170, 550, 660):
            d.line((360, 945, x, 1280), fill=(46, 39, 59), width=2)
        for i in range(4):
            x, y, w, h = 22 + i * 65, 415 + i * 44, 57 - i * 8, 586 - i * 116
            d.rectangle((x, y, x + w, y + h), fill=(18, 19, 34), outline=line, width=2)
            d.rectangle((720 - x - w, y, 720 - x, y + h), fill=(15, 17, 30), outline=line, width=2)
            if setting == "school":
                for v in range(3):
                    d.line((x + 12, y + 30 + v * 8, x + w - 10, y + 30 + v * 8), fill=(68, 68, 90))
        for i in range(3):
            x, y = 320 + i * 10, 420 + i * 62
            d.rectangle((x, y, 720 - x, y + 9), fill=(160, 167, 161))
        d.text((314, 590), "EXIT", font=_font(22), fill=(111, 156, 123))
    elif setting == "street":
        for i, x in enumerate((-80, 170, 495)):
            y = 670 + i % 2 * 72
            d.rectangle((x, y, x + 230, 1000), fill=wall, outline=line, width=2)
            d.polygon([(x - 12, y), (x + 115, y - 120), (x + 242, y)], fill=(16, 15, 29), outline=line)
            for wx in (x + 35, x + 147):
                d.rectangle((wx, y + 60, wx + 45, y + 125), fill=(71, 62, 88), outline=(100, 88, 108), width=2)
        d.polygon([(120, 1000), (560, 1000), (720, 1280), (0, 1280)], fill=(25, 24, 37))
        d.line((604, 1000, 604, 575), fill=(75, 73, 92), width=7)
        d.line((604, 575, 548, 575), fill=(75, 73, 92), width=5)
        d.ellipse((532, 572, 563, 584), fill=(203, 196, 153))
        d.polygon([(542, 587), (555, 587), (670, 1000), (425, 1000)], fill=(47, 43, 51))
    elif setting == "forest":
        for i in range(13):
            x = (i * 117 + 12) % 780 - 30
            base, peak = 990 + i % 3 * 36, 460 + (i * 43) % 230
            width = 65 + i % 4 * 13
            d.rectangle((x - 8, peak + 40, x + 8, base), fill=(10, 16, 23))
            for j in range(4):
                yy = peak + j * 75
                d.polygon([(x, yy), (x - width - j * 8, yy + 160), (x + width + j * 8, yy + 160)], fill=(15 + i % 2 * 7, 22, 34))
        d.polygon([(310, 990), (410, 990), (590, 1280), (100, 1280)], fill=(38, 32, 45))
    else:
        for i in range(12):
            x, y = i * 65 - 20, 790 + (i * 71) % 130
            d.rectangle((x, y, x + 68, 1020), fill=(22, 20, 36), outline=(43, 39, 57), width=1)
            for wy in range(y + 20, 990, 28):
                for wx in range(x + 12, x + 60, 22):
                    d.rectangle((wx, wy, wx + 5, wy + 7), fill=(79, 67, 91))
        d.rectangle((0, 995, 720, 1040), fill=(53, 46, 65), outline=line, width=2)
        d.rectangle((0, 1040, 720, 1280), fill=(23, 21, 33))
    return image


def _window_world(d, backdrop):
    """A fixed window frame opens onto the scene's planned alternate world."""
    if backdrop == "forest":
        d.rectangle((445, 450, 659, 769), fill=(12, 25, 29))
        d.ellipse((569, 477, 604, 512), fill=(157, 189, 170))
        for i in range(8):
            x, y = 449 + i * 30, 556 + i % 3 * 30
            d.rectangle((x - 3, y + 45, x + 3, 769), fill=(37, 48, 43))
            for j in range(3):
                d.polygon([(x, y + j * 37), (x - 24 - j * 3, y + 90 + j * 37),
                           (x + 24 + j * 3, y + 90 + j * 37)], fill=(21 + i % 2 * 8, 47 + i % 3 * 4, 40))
        d.rectangle((445, 748, 659, 769), fill=(19, 35, 29))
    elif backdrop == "city":
        d.rectangle((445, 450, 659, 769), fill=(22, 18, 40))
        for i in range(7):
            x, y = 445 + i * 31, 547 + (i * 39) % 90
            d.rectangle((x, y, min(659, x + 29), 769), fill=(32 + i % 2 * 10, 29, 50 + i % 3 * 6))
            for wx in range(x + 6, min(659, x + 25), 10):
                for wy in range(y + 12, 751, 24):
                    d.rectangle((wx, wy, min(659, wx + 3), wy + 6), fill=(162, 138, 126) if (wx + wy) % 3 else (91, 119, 140))
        d.ellipse((563, 477, 595, 509), fill=(200, 191, 219))
    elif backdrop == "desert":
        d.rectangle((445, 450, 659, 769), fill=(44, 25, 49))
        d.ellipse((576, 491, 620, 535), fill=(207, 156, 118))
        d.polygon([(445, 683), (495, 640), (555, 674), (610, 631), (659, 658), (659, 769), (445, 769)], fill=(102, 63, 65))
        d.polygon([(445, 735), (509, 685), (558, 706), (604, 680), (659, 705), (659, 769), (445, 769)], fill=(69, 45, 59))
        d.line((471, 712, 471, 672), fill=(29, 24, 37), width=3)
        d.line((471, 688, 459, 681, 459, 673), fill=(29, 24, 37), width=3)
        d.line((471, 693, 483, 686, 483, 679), fill=(29, 24, 37), width=3)
    elif backdrop == "otherworld":
        d.rectangle((445, 450, 659, 769), fill=(18, 19, 45))
        d.ellipse((490, 480, 616, 603), fill=(59, 60, 101), outline=(113, 139, 159), width=3)
        d.ellipse((516, 505, 590, 578), fill=(101, 117, 147))
        for i in range(5):
            x, y = 449 + i * 42, 637 + i % 3 * 22
            d.polygon([(x, y), (min(659, x + 35), y), (min(659, x + 19), y + 42)], fill=(86, 67, 110))
            d.rectangle((x + 6, y - 31, min(659, x + 26), y), fill=(44, 48, 75), outline=(85, 107, 126), width=1)
        d.rectangle((445, 751, 659, 769), fill=(32, 30, 58))
    else:
        d.ellipse((565, 475, 615, 525), fill=(204, 206, 220))
        d.polygon([(445, 690), (505, 590), (566, 690), (616, 625), (659, 700)], fill=(45, 41, 73))


def _window_motion(image, scene, local, opening=0.):
    if scene.get("setting", "bedroom") != "bedroom":
        return
    Image, ImageDraw, _, _ = _pil()
    layer = Image.new("RGBA", (214, 319))
    d = ImageDraw.Draw(layer)
    backdrop = scene.get("backdrop", "night")
    if backdrop == "forest":
        sway = math.sin(local * .9) * 6
        d.line((154, 319, 146, 230, 150 + sway, 175), fill=(42, 69, 52, 200), width=4)
        d.line((146, 230, 129 + sway, 205), fill=(42, 69, 52, 200), width=3)
    elif backdrop == "desert":
        shift = local * 10 % 40
        for i in range(6):
            y = 231 + i * 12
            x = (i * 41 + shift) % 170
            d.line((x, y, x + 35, y - 3), fill=(179, 121, 99, 90), width=1)
    elif backdrop == "otherworld":
        y = 176 + math.sin(local * 1.1) * 7
        d.ellipse((103, y, 128, y + 7), outline=(134, 177, 188, 190), width=2)
        for i in range(5):
            x = (i * 47 + local * (4 + i)) % 210
            yy = 70 + (i * 71) % 180
            d.ellipse((x, yy, x + 2, yy + 2), fill=(151, 163, 194, 170))
    elif backdrop == "city":
        x = 30 + local * 13 % 160
        d.line((x, 302, x + 13, 302), fill=(223, 162, 135, 200), width=2)
        d.line((0, 242 + math.sin(local * .5) * 4, 214, 242 + math.sin(local * .5) * 4), fill=(113, 104, 151, 42), width=4)
    else:
        x = (local * 8) % 180 + 10
        d.ellipse((x, 151, x + 2, 153), fill=(183, 188, 216, 170))
    image.paste(layer, (445, 450), layer)
    # The worlds and their motion remain behind the same mullions.
    d = ImageDraw.Draw(image)
    mullion = 552 - 104 * opening
    d.line((mullion, 445, mullion, 775), fill=(88, 86, 116), width=5)
    d.line((440, 610, 664 - 210 * opening, 610), fill=(88, 86, 116), width=5)


def _toward(first, last, amount):
    return (first[0] + (last[0] - first[0]) * amount, first[1] + (last[1] - first[1]) * amount)


def _joint(root, end, upper, lower, bend=1):
    """Two-bone inverse kinematics, clamped to physically reachable lengths."""
    dx, dy = end[0] - root[0], end[1] - root[1]
    raw = max(.0001, math.hypot(dx, dy))
    distance = min(upper + lower - .001, max(abs(upper - lower) + .001, raw))
    clamped_end = (root[0] + dx / raw * distance, root[1] + dy / raw * distance)
    angle = math.atan2(dy, dx)
    offset = math.acos(max(-1., min(1., (upper * upper + distance * distance - lower * lower) / (2 * upper * distance))))
    elbow = (root[0] + upper * math.cos(angle + bend * offset), root[1] + upper * math.sin(angle + bend * offset))
    return elbow, clamped_end


def _portable(draw, kind, x, y, scale, direction=1, alpha=1.):
    s, a = scale, round(255 * max(0., min(1., alpha)))
    if kind == "phone":
        draw.rounded_rectangle((x - 10 * s, y - 16 * s, x + 10 * s, y + 16 * s), radius=3 * s, fill=(25, 28, 44, a), outline=(184, 194, 214, a), width=max(1, round(2 * s)))
        draw.rectangle((x - 6 * s, y - 11 * s, x + 6 * s, y + 8 * s), fill=(112, 162, 185, a))
    elif kind == "key":
        draw.ellipse((x - 14 * s, y - 7 * s, x, y + 7 * s), outline=(222, 190, 108, a), width=max(2, round(3 * s)))
        draw.line((x, y, x + 19 * s, y), fill=(222, 190, 108, a), width=max(2, round(3 * s)))
        draw.line((x + 14 * s, y, x + 14 * s, y + 7 * s), fill=(222, 190, 108, a), width=max(2, round(3 * s)))
    elif kind == "book":
        draw.rectangle((x - 19 * s, y - 13 * s, x + 19 * s, y + 13 * s), fill=(95, 66, 123, a), outline=(205, 185, 219, a), width=max(1, round(2 * s)))
        draw.line((x, y - 12 * s, x, y + 12 * s), fill=(225, 215, 228, a), width=max(1, round(2 * s)))
    elif kind == "flashlight":
        tip = x + direction * 18 * s
        draw.polygon([(tip, y - 7 * s), (tip + direction * 130 * s, y - 45 * s), (tip + direction * 130 * s, y + 45 * s), (tip, y + 7 * s)], fill=(214, 210, 164, round(a * .10)))
        draw.rectangle((x - 14 * s, y - 6 * s, x + 14 * s, y + 6 * s), fill=(101, 115, 133, a), outline=(180, 196, 206, a), width=max(1, round(2 * s)))
        draw.ellipse((tip - 4 * s, y - 8 * s, tip + 4 * s, y + 8 * s), fill=(240, 230, 178, a))


def _character(image, char, state, seconds):
    """Draw the persistent rig with eased pose, gaze, expressions and props."""
    Image, ImageDraw, _, _ = _pil()
    visibility = state["visibility"]
    if visibility < .003:
        return
    layer = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(layer)
    x, foot, s = state["x"], state["y"], state["scale"]
    shadow = bool(char.get("shadow")) or "shadow" in (str(char.get("name", "")) + str(char.get("role", ""))).lower()
    line = (129, 122, 149) if shadow else (235, 233, 247)
    fill = (5, 6, 14) if shadow else (19, 17, 32)
    thick = max(2, round((5.5 if not shadow else 6.5) * s))
    accent = _accent(char)
    swing = math.sin(state["phase"]) * state["walk"]
    bob = abs(math.sin(state["phase"] * 2)) * 3 * state["walk"] + math.sin(seconds * 1.7) * 1.4
    tremble = math.sin(seconds * 15) * state["reaction"] * 2.5
    lean = state["lean"] * 26 * s + tremble
    hip = (x + lean * .3, foot - 100 * s + state["crouch"] * 42 * s + bob)
    shoulder = (x + lean, hip[1] - 94 * s + state["crouch"] * 47 * s)
    head = (shoulder[0] + state["facing"] * 3 * s, shoulder[1] - 50 * s)
    draw.ellipse((x - 49 * s, foot - 8 * s, x + 55 * s, foot + 10 * s), fill=(8, 9, 17, 210))
    hands = {}
    for side in (-1, 1):
        lift = max(0., math.sin(state["phase"] + (math.pi if side == -1 else 0))) * 12 * state["walk"]
        ankle = (x + side * 19 * s + swing * side * 16 * s, foot - lift * s)
        knee, ankle = _joint(hip, ankle, 53 * s, 53 * s, bend=side)
        draw.line((hip, knee, ankle), fill=line, width=thick, joint="curve")
        hand = (shoulder[0] + side * 30 * s - swing * side * 17 * s, shoulder[1] + 84 * s)
        forward = side == (1 if state["facing"] >= 0 else -1)
        if forward:
            hand = _toward(hand, (state["target_x"], state["target_y"]), state["reach"])
            target = (shoulder[0] + side * 96 * s, shoulder[1] - 8 * s)
            hand = _toward(hand, target, state["point"])
        hand = _toward(hand, (head[0] + side * 17 * s, head[1] + 20 * s), state["guard"])
        hand = _toward(hand, (shoulder[0] + side * 66 * s, shoulder[1] - 17 * s), state["reaction"] * .65)
        hand = _toward(hand, (shoulder[0] + side * 13 * s, shoulder[1] + 57 * s), state["holding"] * .85)
        elbow, hand = _joint(shoulder, hand, 48 * s, 49 * s, bend=side)
        draw.line((shoulder, elbow, hand), fill=line, width=thick, joint="curve")
        hands[side] = hand
    draw.line((shoulder, hip), fill=line, width=thick + 1)
    radius = 32 * s
    draw.ellipse((head[0] - radius, head[1] - radius, head[0] + radius, head[1] + radius), fill=fill, outline=line, width=thick)
    blink = .12 if seconds % 4.7 > 4.56 else 1.
    gaze = state["facing"] * 7 * s
    for side in (-1, 1):
        xx, yy = head[0] + gaze + side * 10 * s, head[1]
        eye_h = max(.6, 2.5 * s * state["eyes"] * blink)
        draw.ellipse((xx - 2 * s, yy - eye_h, xx + 2 * s, yy + eye_h), fill=(211, 105, 119) if shadow else (247, 245, 252))
        brow = state["brow"] * side * 6 * s
        draw.line((xx - 5 * s, yy - 10 * s + brow, xx + 5 * s, yy - 10 * s - brow), fill=line, width=max(1, round(2 * s)))
    if state["mouth"] > .25:
        opening = state["mouth"] * 8 * s
        draw.ellipse((head[0] + gaze - 5 * s, head[1] + 13 * s - opening / 2,
                      head[0] + gaze + 5 * s, head[1] + 13 * s + opening / 2), outline=line, width=max(1, round(2 * s)))
    else:
        draw.line((head[0] + gaze - 5 * s, head[1] + 15 * s, head[0] + gaze + 5 * s, head[1] + 15 * s), fill=(178, 169, 194), width=max(1, round(2 * s)))
    accessory = str(char.get("accessory", "none")).lower()
    if accessory == "cap":
        draw.rectangle((head[0] - 30 * s, head[1] - 41 * s, head[0] + 30 * s, head[1] - 28 * s), fill=accent)
        draw.line((head[0] - 36 * s, head[1] - 28 * s, head[0] + 39 * s, head[1] - 28 * s), fill=accent, width=max(2, round(5 * s)))
    elif accessory == "backpack":
        draw.rounded_rectangle((shoulder[0] - 27 * s, shoulder[1] + 4 * s, shoulder[0] - 3 * s, shoulder[1] + 68 * s), radius=5 * s, fill=accent)
    elif accessory == "glasses":
        for side in (-1, 1):
            xx = head[0] + gaze + side * 13 * s
            draw.rectangle((xx - 9 * s, head[1] - 5 * s, xx + 9 * s, head[1] + 8 * s), outline=accent, width=max(1, round(2 * s)))
    elif accessory == "scarf":
        draw.line((shoulder[0] - 14 * s, shoulder[1] - 12 * s, shoulder[0] + 16 * s, shoulder[1] - 8 * s), fill=accent, width=max(3, round(8 * s)))
        draw.line((shoulder[0] + 9 * s, shoulder[1] - 5 * s, shoulder[0] + 18 * s, shoulder[1] + 30 * s), fill=accent, width=max(3, round(7 * s)))
    if state["holding"] > .001:
        hand = hands[1 if state["facing"] >= 0 else -1]
        _portable(draw, state["prop_kind"], hand[0], hand[1] - 6 * s, s, 1 if state["facing"] >= 0 else -1, state["holding"])
    if visibility < .999:
        layer.putalpha(layer.getchannel("A").point(lambda a: round(a * visibility)))
    image.paste(layer, (0, 0), layer)



def _captions(image, scene, time_s):
    Image, ImageDraw, _, _ = _pil()
    words = scene.get("words", [])
    if not words:
        return image
    active = next((i for i, w in enumerate(words) if float(w.get("start_s", 0)) <= time_s < float(w.get("end_s", 0))), None)
    if active is None:
        return image
    # Stable short phrase groups fit the safe top area, with only the current
    # spoken word highlighted. All glyphs are raster text, never FFmpeg code.
    first = active // 5 * 5
    chunk = words[first:first + 5]
    phrase_timing = words[active].get("timing") == "phrase"
    if phrase_timing:
        # Some offline voices supply only a real phrase interval. Display that
        # phrase together, without inventing word-level speech alignment.
        chunk = [words[active]]
    font = _font(33 if phrase_timing else 41)
    layer = Image.new("RGBA", image.size)
    d = ImageDraw.Draw(layer)
    rows, row, row_width = [], [], 0
    space = d.textlength(" ", font=font)
    tokens = [(str(w.get("text", ""))[:80], first + i) for i, w in enumerate(chunk)]
    if phrase_timing:
        tokens = [(text[:80], -1) for text in str(chunk[0].get("text", "")).split()[:70]]
    for text, index in tokens:
        ww = d.textlength(text, font=font)
        if row and row_width + ww + space > 600:
            rows.append(row); row, row_width = [], 0
        row.append((text, index, ww)); row_width += ww + space
    if row:
        rows.append(row)
    progress = _smooth((time_s - float(chunk[0].get("start_s", 0))) / .10)
    y = 155 + (1 - progress) * 7
    for row in rows[:5 if phrase_timing else 3]:
        full_width = sum(w for _, _, w in row) + space * (len(row) - 1)
        x = (BASE_W - full_width) / 2
        for text, idx, ww in row:
            emphasis = any(v in text.lower() for v in ("never", "shadow", "door", "window", "alone", "behind", "missing", "night"))
            fill = (255, 111, 119, 255) if emphasis and (idx == active or phrase_timing) else (255, 220, 104, 255) if idx == active else (250, 248, 253, 255)
            d.text((x, y), text, font=font, fill=fill, stroke_width=3, stroke_fill=(6, 5, 13, 235))
            x += ww + space
        y += 44 if phrase_timing else 53
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


class FrameRenderer:
    def __init__(self, project, width=720):
        if width not in {720, 1080}:
            raise ValueError("Story quality must be 720p or 1080p.")
        self.project, self.width = project, width
        self.scenes = project.get("scenes", [])
        if not self.scenes:
            raise ValueError("Plan at least one story scene before rendering.")
        self.characters = {c["id"]: c for c in project.get("characters", [])}
        if not self.characters:
            raise ValueError("This story needs its saved character library.")
        self.backgrounds = {}
        self.starts = [s["start_s"] for s in self.scenes]
        self.motion = MotionPlan(project)

    @staticmethod
    def _world_key(scene):
        return (str(scene.get("setting", "bedroom")), str(scene.get("lighting", "normal")),
                str(scene.get("backdrop", "night")))

    def _world(self, scene, seconds):
        Image, ImageDraw, _, _ = _pil()
        key = self._world_key(scene)
        if key not in self.backgrounds:
            self.backgrounds[key] = _background(scene)
        image = self.backgrounds[key].copy()
        setting = scene.get("setting", "bedroom")
        if setting == "bedroom":
            # Redraw only the small opening so its moving pane can reveal the
            # world behind it while the fixed room and frame remain continuous.
            d = ImageDraw.Draw(image)
            d.rectangle((445, 450, 659, 769), fill=(7, 13, 30))
            _window_world(d, scene.get("backdrop", "night"))
            _window_motion(image, scene, seconds, self.motion.object(setting, "window", seconds))
            d = ImageDraw.Draw(image)
            opening = self.motion.object(setting, "door", seconds)
            right = 290 - 128 * opening
            d.rectangle((141, 407, 289, 1008), fill=(5, 7, 16))
            d.polygon([(142, 408), (right, 408 + 35 * opening), (right, 1008 - 38 * opening), (142, 1008)],
                      fill=(34, 28, 49), outline=(81, 70, 100))
            d.ellipse((right - 23, 791, right - 16, 798), fill=(183, 151, 93))
            if opening > .03:
                d.line((right + 3, 450, right + 3, 974), fill=(79, 91, 122), width=2)
        elif setting in {"hallway", "school"}:
            opening = self.motion.object(setting, "door", seconds)
            right = 434 - 128 * opening
            d = ImageDraw.Draw(image)
            d.rectangle((287, 577, 433, 943), fill=(6, 8, 16))
            d.polygon([(288, 578), (right, 578 + 16 * opening), (right, 943 - 17 * opening), (288, 943)],
                      fill=(23, 25, 39), outline=(58, 57, 78))
            d.ellipse((right - 20, 797, right - 14, 803), fill=(140, 145, 153))
        prop = inferred_prop(scene)
        if prop != "none":
            holding = max((self.motion.character(cid, seconds)["holding"]
                           for cid in scene.get("character_ids", []) if cid in self.characters), default=0.)
            layer = Image.new("RGBA", image.size)
            _portable(ImageDraw.Draw(layer), prop, 430, 1038, 1., alpha=1 - holding)
            image.paste(layer, (0, 0), layer)
        return image

    def frame(self, time_s):
        Image, ImageDraw, _, _ = _pil()
        index = max(0, min(len(self.scenes) - 1, bisect.bisect_right(self.starts, time_s) - 1))
        scene = self.scenes[index]
        previous = self.scenes[index - 1] if index else scene
        transition = smooth((time_s - scene["start_s"]) / .75) if index else 1.
        image = self._world(scene, time_s)
        # Locations and portal worlds dissolve behind one shared set of rigs.
        # Identical locations don't transition at all; narration boundaries are
        # never used as a reason to reset position, phase, camera or exposure.
        if index and self._world_key(scene) != self._world_key(previous) and transition < 1:
            image = Image.blend(self._world(previous, time_s), image, transition)
        draw = ImageDraw.Draw(image)
        for i in range(16):
            x, y = (i * 173 + time_s * (2 + i % 4)) % 720, (i * 71 + 340 + time_s * 3) % 1000 + 130
            draw.ellipse((x, y, x + 1, y + 1), fill=(88, 78, 105))
        actors = [(cid, self.motion.character(cid, time_s)) for cid in self.characters]
        for cid, state in sorted(actors, key=lambda item: item[1]["y"]):
            _character(image, self.characters[cid], state, time_s)
        view = self.motion.view(time_s)
        shake_weight = (1 - transition) * (previous.get("effect") == "shake") + transition * (scene.get("effect") == "shake")
        shake = math.sin(time_s * 46) * 2.5 * shake_weight
        zoom, pan = view["zoom"], view["pan_x"]
        image = image.transform(image.size, Image.Transform.AFFINE,
            (1 / zoom, 0, (BASE_W - BASE_W / zoom) / 2 + pan + shake,
             0, 1 / zoom, (BASE_H - BASE_H / zoom) / 2 + view["pan_y"] + shake), resample=Image.Resampling.BILINEAR)
        flicker = (1 - transition) * (previous.get("lighting") == "flicker") + transition * (scene.get("lighting") == "flicker")
        if flicker:
            dim = (.20 if math.sin(time_s * 10.5) > .93 else .02) * flicker
            image = Image.blend(image, Image.new("RGB", image.size, (0, 0, 0)), dim)
        vision = (1 - transition) * (previous.get("lighting") == "nightvision") + transition * (scene.get("lighting") == "nightvision")
        security = ((1 - transition) * (previous.get("effect") == "security" or previous.get("lighting") == "nightvision")
                    + transition * (scene.get("effect") == "security" or scene.get("lighting") == "nightvision"))
        if vision:
            green = image.convert("L")
            tinted = Image.merge("RGB", (green.point(lambda p: int(p * .53)), green, green.point(lambda p: int(p * .62))))
            image = Image.blend(image, tinted, vision)
        if security:
            overlay = Image.new("RGBA", image.size)
            od = ImageDraw.Draw(overlay)
            for y in range(0, BASE_H, 6):
                od.line((0, y, BASE_W, y), fill=(0, 0, 0, round(30 * security)))
            od.rectangle((28, 300, 692, 1174), outline=(175, 191, 180, round(120 * security)), width=2)
            od.ellipse((53, 324, 64, 335), fill=(233, 91, 106, round(255 * security)))
            od.text((75, 317), "REC  CAM 02", font=_font(21, False), fill=(204, 217, 208, round(220 * security)))
            od.text((465, 1135), f"00:{int(time_s)//60:02}:{int(time_s)%60:02}", font=_font(25, False), fill=(204, 217, 208, round(230 * security)))
            image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        if self.project.get("options", {}).get("captions", True):
            image = _captions(image, scene, time_s)
        if self.width != BASE_W:
            image = image.resize((self.width, self.width * 16 // 9), resample=Image.Resampling.LANCZOS)
        return image



def frame_at(project: dict, time_s: float, width=720):
    """Render one deterministic Pillow image, suitable for a local thumbnail."""
    return FrameRenderer(project, width).frame(time_s)


def render_story(project: dict, audio_path: Path, output_path: Path, settings) -> dict:
    """Encode narrated animation atomically; cancel leaves any prior output intact.

    A bounded writer queue keeps the control thread responsive even if FFmpeg
    stops consuming stdin. Encoder diagnostics go to a temporary disk stream,
    preventing pipe deadlock and unbounded memory usage.
    """
    options = project.get("options", {})
    width, fps = options.get("quality", 720), options.get("fps", 24)
    if isinstance(width, bool) or width not in {720, 1080}:
        raise ValueError("Story quality must be 720p or 1080p.")
    if isinstance(fps, bool) or fps not in {12, 24, 30}:
        raise ValueError("Choose a supported story frame rate: 12, 24 or 30.")
    duration = float(project.get("duration_s", 0))
    if not math.isfinite(duration) or duration <= 0 or duration > 300:
        raise ValueError("Narration must have a valid duration of up to five minutes.")
    audio_path, output_path = Path(audio_path).resolve(), Path(output_path).resolve()
    if not audio_path.is_file():
        raise ValueError("Generate the local narration audio before rendering.")
    if output_path.exists():
        raise ValueError("A story video already exists at this output path; use a new revision.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_name("." + output_path.stem + "." + uuid.uuid4().hex + ".rendering.mp4")
    total = math.ceil(duration * fps)
    height = width * 16 // 9
    renderer = FrameRenderer(project, width)
    args = [settings.render.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
            "-framerate", str(fps), "-i", "pipe:0", "-protocol_whitelist", "file,pipe", "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.6f}", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(staging)]
    frames, errors = queue.Queue(maxsize=2), []
    process = None
    writer = None
    try:
        report("story_rendering", 0, total)
        with tempfile.TemporaryFile() as diagnostics:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=diagnostics)

            def feed():
                try:
                    while True:
                        data = frames.get()
                        if data is None:
                            break
                        process.stdin.write(data)
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError) as error:
                    errors.append(error)

            writer = threading.Thread(target=feed, name="clipforge-story-frames", daemon=True)
            writer.start()

            def send(data):
                while True:
                    checkpoint()
                    if errors or process.poll() is not None:
                        raise ValueError("The local story encoder stopped unexpectedly. Check FFmpeg and available disk space.")
                    try:
                        frames.put(data, timeout=.10)
                        return
                    except queue.Full:
                        pass

            for frame in range(total):
                checkpoint()
                send(renderer.frame(frame / fps).tobytes())
                if frame % max(1, fps // 2) == 0:
                    report("story_rendering", frame, total)
            send(None)
            while process.poll() is None:
                checkpoint()
                time.sleep(.05)
            checkpoint()
            writer.join(timeout=2)
            if process.returncode or errors:
                raise ValueError("The local story encoder could not finish. Check FFmpeg and available disk space.")
            if not staging.is_file() or staging.stat().st_size < 1024:
                raise ValueError("FFmpeg did not create a complete story video.")
            checkpoint()
            os.replace(staging, output_path)
            report("story_rendering", total, total)
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)
        # Unblock a writer waiting for the next frame, then reap it before
        # deleting the in-progress output on Windows.
        if writer is not None and writer.is_alive():
            try:
                frames.put_nowait(None)
            except queue.Full:
                pass
            writer.join(timeout=2)
        staging.unlink(missing_ok=True)
        raise
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                with contextlib.suppress(OSError, ValueError):
                    process.stdin.close()
            if process.poll() is None:
                process.kill(); process.wait()
    return {"width": width, "height": height, "fps": fps, "duration_s": duration,
            "animation_version": ANIMATION_VERSION, "renderer": "procedural_2d"}
