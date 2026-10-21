"""A small, pinned catalog of optional offline neural story narrators.

Only explicit installation contacts the upstream model host. Story text and
voice previews are synthesized locally. Paths and download URLs never come
from browser input; approved files are checked against pinned SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from filelock import FileLock, Timeout

from .common import atomic_json
from .job_control import checkpoint, report
from .story_audio import (StoryAudioError, _piper_command, _read_pcm,
                          discover_voices, refresh_voices, synthesize_story)

REVISION = "5041a7a49ae5b3a0f30e6837769eb03fdbb231bd"
REPOSITORY = "https://huggingface.co/rhasspy/piper-voices"
ENGINE_SOURCE = "https://github.com/OHF-Voice/piper1-gpl"
MODEL_LICENSE_URL = REPOSITORY + "/blob/" + REVISION + "/LICENSE"
PREVIEW_TEXT = "At midnight, a window opened into another world. This is your local ClipForge narrator."

# Verified against the upstream repository's LFS OIDs, configuration bytes and
# model cards. The top-level MIT repository license and each card's dataset
# terms are shown separately; they are not blanket claims about speech rights.
_PACKS = (
    {"id": "en_US-ljspeech-medium", "name": "LJ · American English", "language": "en-US",
     "description": "A clear female narrator for calm stories and suspense.",
     "dataset_license": "Public domain", "dataset_url": "https://keithito.com/LJ-Speech-Dataset/",
     "attribution": "Piper LJSpeech medium by Bryce Beattie and Rhasspy contributors; trained from scratch on the public-domain LJ Speech dataset. See the bundled upstream model card.",
     "location": "en/en_US/ljspeech/medium",
     "files": (("MODEL_CARD", 517, "fbee1529c89d36b3fe76d7e9f3f832dce17f44900a52d76a9bda735654766b4d"),
               ("en_US-ljspeech-medium.onnx", 63531379, "6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22"),
               ("en_US-ljspeech-medium.onnx.json", 4972, "141d612cc0a95ed7efc1ca936b845c2364967f2e9217c5dbfcf69fc4d6c65860"))},
    {"id": "en_US-norman-medium", "name": "Norman · American English", "language": "en-US",
     "description": "A measured male narrator for mysteries and longer storytelling.",
     "dataset_license": "Public domain", "dataset_url": "https://librivox.org/",
     "attribution": "Piper Norman medium by Bryce Beattie and Rhasspy contributors; trained from scratch using public-domain LibriVox recordings. See the bundled upstream model card.",
     "location": "en/en_US/norman/medium",
     "files": (("MODEL_CARD", 528, "60eaf4d10009d1bd4a1aa555d337dae00f60421d72fee415b9139a021e7cb5d0"),
               ("en_US-norman-medium.onnx", 63531379, "b9739443232a80a59c7d18810dd856899bf16a7964725f5ab81ea49b1351cb71"),
               ("en_US-norman-medium.onnx.json", 4968, "6c2db7f558a4a8deb9fe822583c1c5105f6c4e834dd0f9de8ad17a888ee9fe1d"))},
    {"id": "en_US-joe-medium", "name": "Joe · American English", "language": "en-US",
     "description": "An everyday male voice for conversational narration.",
     "dataset_license": "CC0", "dataset_url": "https://github.com/OHF-Voice/voice-datasets",
     "attribution": "Piper Joe medium by Michael Hansen / Rhasspy contributors; CC0 voice dataset from OHF-Voice. Fine-tuned from Lessac medium, as documented by its upstream model card.",
     "location": "en/en_US/joe/medium",
     "files": (("MODEL_CARD", 281, "d2caa63aca0fccb155105e959e393e5c0c0f03a1f388ef5ba217b83ef860c760"),
               ("en_US-joe-medium.onnx", 63201294, "58afce0321b8d9c46d7cdf9c16500cc55a793b4220212dba6b70fb788b3baf06"),
               ("en_US-joe-medium.onnx.json", 4794, "3d6d5410b3795cb1950595247ef8f06190719e6fdbfa3a2356d8ec368e1aad33"))},
    {"id": "en_GB-alba-medium", "name": "Alba · British English", "language": "en-GB",
     "description": "A female British English voice for a different storytelling sound.",
     "dataset_license": "CC BY 4.0", "dataset_url": "https://datashare.ed.ac.uk/handle/10283/3270",
     "attribution": "Piper Alba medium by Michael Hansen / Rhasspy contributors. Dataset: University of Edinburgh data record 10283/3270, CC BY 4.0. Fine-tuned from Lessac medium; retain this credit and the upstream model card.",
     "location": "en/en_GB/alba/medium",
     "files": (("MODEL_CARD", 324, "fa166b1779404c470b0b6b4ba0238bc4a35bf89d2cd130c6788f697188b737d6"),
               ("en_GB-alba-medium.onnx", 63201294, "401369c4a81d09fdd86c32c5c864440811dbdcc66466cde2d64f7133a66ad03b"),
               ("en_GB-alba-medium.onnx.json", 4888, "aa965a2f02ecced632c2694e1fc72bbff6d65f265fab567ca945918c73dd89f4"))},
)


def _redirected(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _model_root(settings, create=False) -> Path:
    workspace = settings.resolve(".")
    directory = workspace
    for part in ("models", "piper"):
        directory = directory / part
        if _redirected(directory) or (directory.exists() and not directory.is_dir()):
            raise StoryAudioError("The local voice folder must be a regular folder inside this ClipForge workspace.")
        if create:
            directory.mkdir(exist_ok=True)
    return directory


def _safe_file(path: Path):
    if _redirected(path) or (path.exists() and not path.is_file()):
        raise StoryAudioError("A voice destination is redirected or is not a regular file. Choose a regular local models/piper folder.")


def _pack(catalog_id: str) -> dict:
    for item in _PACKS:
        if item["id"] == catalog_id:
            return item
    raise StoryAudioError("Choose a voice from ClipForge's local narrator catalog.")


def _local_name(pack, filename):
    return pack["id"] + ".MODEL_CARD" if filename == "MODEL_CARD" else filename


def _catalog_record(pack, root):
    filenames = (pack["id"] + ".onnx", pack["id"] + ".onnx.json")
    expected = {name: size for name, size, _ in pack["files"]}
    installed = all((root / name).is_file() and not _redirected(root / name)
                    and (root / name).stat().st_size == expected[name] for name in filenames)
    return {"id": pack["id"], "voice_id": "piper:" + pack["id"] + ".onnx",
            "name": pack["name"], "language": pack["language"], "description": pack["description"],
            "quality": "medium", "download_bytes": sum(size for _, size, _ in pack["files"]),
            "installed": installed, "license": "MIT model repository; dataset: " + pack["dataset_license"],
            "license_url": MODEL_LICENSE_URL, "dataset_license": pack["dataset_license"],
            "dataset_url": pack["dataset_url"], "attribution": pack["attribution"],
            "source_url": REPOSITORY + "/tree/" + REVISION + "/" + pack["location"],
            "model_card_url": REPOSITORY + "/blob/" + REVISION + "/" + pack["location"] + "/MODEL_CARD"}


def voice_library(settings) -> dict:
    """A local-only catalog read; never downloads or enumerates remote models."""
    root = _model_root(settings)
    engine = bool(_piper_command(settings))
    catalog = [_catalog_record(pack, root) for pack in _PACKS]
    names = {item["voice_id"]: item for item in catalog}
    voices = discover_voices(settings)
    for voice in voices:
        pack = names.get(voice["id"])
        if pack:
            voice.update(name=pack["name"], description=pack["description"], license=pack["license"],
                         attribution=pack["attribution"], model_card_url=pack["model_card_url"])
    return {"voices": voices, "catalog": catalog, "engine_ready": engine,
            "engine_message": "Local neural narration is ready. Voice packs download once; narration stays on this computer." if engine else
                "Install ClipForge's optional voices support with: python -m pip install -e .[voices]. Installed Windows desktop voices still work.",
            "engine_source_url": ENGINE_SOURCE,
            "privacy_note": "Installing contacts the official model host to download the chosen voice. Your scripts and previews stay local."}


def _allowed_download(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and (host == "huggingface.co" or host.endswith(".huggingface.co")
                     or host == "hf.co" or host.endswith(".hf.co")))
    except ValueError:
        return False


def _download(session, url, destination, expected_size, expected_sha, completed, total):
    response = None
    try:
        for _ in range(6):
            checkpoint()
            if not _allowed_download(url):
                raise StoryAudioError("The model host returned an unsupported download address. No voice was installed.")
            response = session.get(url, stream=True, timeout=(10, 10), allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                response.close()
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise StoryAudioError("The official voice download is unavailable. Check your connection and retry.")
            break
        else:
            raise StoryAudioError("The official voice host redirected too many times. Try installing again later.")
        count = 0
        digest = hashlib.sha256()
        with destination.open("xb") as stream:
            for block in response.iter_content(chunk_size=256 * 1024):
                checkpoint()
                if not block:
                    continue
                count += len(block)
                if count > expected_size:
                    raise StoryAudioError("The voice download exceeded its verified size. No voice was installed.")
                digest.update(block)
                stream.write(block)
                report("story_voice_download", completed + count, total)
        if count != expected_size or digest.hexdigest() != expected_sha:
            raise StoryAudioError("The voice download failed its integrity check. Retry to download a clean copy.")
        return count
    finally:
        if response is not None:
            response.close()


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checkpoint()
            digest.update(block)
    return digest.hexdigest()


def install_voice(catalog_id: str, settings) -> dict:
    """Install one approved pack atomically; cancellation never exposes parts."""
    pack = _pack(catalog_id)
    if not _piper_command(settings):
        raise StoryAudioError("Install ClipForge's optional voices support first: python -m pip install -e .[voices].")
    root = _model_root(settings, create=True)
    lockpath = root / ".voice-install.lock"
    _safe_file(lockpath)
    try:
        with FileLock(str(lockpath), timeout=0):
            for filename, _, _ in pack["files"]:
                _safe_file(root / _local_name(pack, filename))
            record_path = root / (pack["id"] + ".license.json")
            _safe_file(record_path)
            if all((root / _local_name(pack, filename)).is_file()
                   and (root / _local_name(pack, filename)).stat().st_size == size
                   and _hash(root / _local_name(pack, filename)) == digest
                   for filename, size, digest in pack["files"]):
                refresh_voices(settings)
                return {"voice_id": "piper:" + pack["id"] + ".onnx", "installed": True,
                        "already_installed": True, "catalog": _catalog_record(pack, root)}
            total = sum(size for _, size, _ in pack["files"])
            report("story_voice_download", 0, total)
            with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as temporary:
                staging = Path(temporary)
                downloaded = 0
                with requests.Session() as session:
                    session.trust_env = False
                    for filename, size, digest in pack["files"]:
                        url = REPOSITORY + "/resolve/" + REVISION + "/" + pack["location"] + "/" + filename
                        downloaded += _download(session, url, staging / _local_name(pack, filename),
                                                size, digest, downloaded, total)
                config = json.loads((staging / (pack["id"] + ".onnx.json")).read_text(encoding="utf-8"))
                if config.get("num_speakers") != 1 or not isinstance(config.get("audio"), dict):
                    raise StoryAudioError("The official voice configuration is incompatible with this local narrator.")
                atomic_json(staging / record_path.name, {**_catalog_record(pack, root), "installed": True, "revision": REVISION,
                            "files": [{"name": n, "size": s, "sha256": h} for n, s, h in pack["files"]]})
                checkpoint()
                # Publish model last. If a disk operation fails, restore any
                # previous pack instead of leaving an incomplete replacement.
                names = [pack["id"] + ".onnx.json", pack["id"] + ".MODEL_CARD", record_path.name, pack["id"] + ".onnx"]
                backups, installed = [], []
                try:
                    for name in names:
                        target = root / name
                        _safe_file(target)
                        if target.exists():
                            os.replace(target, staging / (name + ".previous"))
                            backups.append(name)
                        os.replace(staging / name, target)
                        installed.append(name)
                except OSError:
                    for name in reversed(installed):
                        (root / name).unlink(missing_ok=True)
                    for name in backups:
                        os.replace(staging / (name + ".previous"), root / name)
                    raise
            refresh_voices(settings)
            return {"voice_id": "piper:" + pack["id"] + ".onnx", "installed": True,
                    "already_installed": False, "catalog": _catalog_record(pack, root)}
    except Timeout:
        raise StoryAudioError("Another voice is being installed. Wait for it to finish, then retry.") from None
    except requests.RequestException:
        raise StoryAudioError("The voice download was interrupted. Check your connection and retry; existing voices are kept.") from None
    except (OSError, ValueError) as error:
        if isinstance(error, StoryAudioError):
            raise
        raise StoryAudioError("The voice could not be saved. Check free disk space and the local models/piper folder, then retry.") from error


def preview_voice(voice_id: str, settings, folder: Path) -> Path:
    """Generate a short, fixed local sample, never arbitrary remote content."""
    if not isinstance(voice_id, str) or voice_id not in {v["id"] for v in discover_voices(settings)}:
        raise StoryAudioError("Choose an installed local narrator to preview.")
    folder = Path(folder)
    if _redirected(folder) or not folder.resolve().is_relative_to(settings.resolve(".")):
        raise StoryAudioError("Voice previews must be saved inside this local ClipForge workspace.")
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "preview.wav"
    _safe_file(output)
    report("story_voice_preview")
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=folder) as temporary:
        result = synthesize_story({"options": {"voice": voice_id, "music": False, "effects": False, "duration": 30},
                    "scenes": [{"id": "preview", "narration": PREVIEW_TEXT, "setting": "bedroom", "action": "idle"}]},
                    Path(temporary), settings)
        _read_pcm(Path(result["audio_path"]))
        checkpoint()
        os.replace(result["audio_path"], output)
    report("story_voice_preview", 1, 1)
    return output
