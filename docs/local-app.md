# ClipForge browser app

ClipForge's local server opens a studio in your browser at `http://127.0.0.1:8000/`. Your own computer performs video processing and stores the results. This version runs as a Python application; the launchers prepare its basic environment, while FFmpeg and optional AI tools are separate installations.

## Start and stop

Download the repository ZIP from GitHub and extract all of it into a writable folder. Install Python 3.11 or newer and [FFmpeg with ffprobe](https://ffmpeg.org/download.html). Choose an FFmpeg build with libx264, AAC and libass. Windows users should enable **Add Python to PATH** when installing Python.

- **Windows:** double-click `Start-ClipForge.bat`.
- **macOS/Linux:** open a terminal in the project folder and run `bash ./start-clipforge.sh`.

The first launch creates `.venv` and installs the basic Python dependencies from the package index. It needs internet access for those downloads. Later launches reuse that environment. No speech models, Ollama models or FFmpeg binaries are downloaded by the launcher.

Keep the launcher terminal open while using the app. Closing only the browser tab leaves the server running. Press **Ctrl+C** in the terminal to stop it; on Windows, confirm the batch termination prompt if one appears. Let an active render finish first. A job interrupted by shutdown can be retried from the app after restarting.

If the browser does not open, visit `http://127.0.0.1:8000/` yourself. `http://localhost:8000/` also refers to this computer. To use another port or stop the automatic browser opening:

```powershell
# Windows, from the project folder
.\Start-ClipForge.bat --port 8001 --no-browser
```

```sh
# macOS/Linux, from the project folder
bash ./start-clipforge.sh --port 8001 --no-browser
```

Users with an existing Python environment can run the server directly:

```sh
python -m pip install -e .
python -m clipforge serve
```

The server binds to `127.0.0.1`, so the app is available on this computer. Keep one ClipForge server open for each workspace. Browser reloads preserve the saved library and jobs.

## Make your first clip

1. Open **Settings** and check the prerequisites. The manual editor needs FFmpeg and ffprobe. Enter their full paths if your installed executables are not on PATH, save, and check again.
2. Open **Studio** and add a video. Browser upload copies a file into ClipForge's workspace. Importing an existing local file by its full path registers that file in place; keep it at that path while it is in use. **Try a sample** registers the included synthetic test pattern and tones.
3. Choose **Pick a moment** for manual mode, enter a title, and set a start and end inside the source. A manual cut can be 1–180 seconds. Confirm you own the recording or have permission to edit it, and complete its rights/credit information.
4. Choose framing: **center crop** fills the vertical frame and trims the sides; **fit** preserves the whole image with padding; **blur** fits the image over a blurred background. Optional manual text is a static overlay for the entire cut. It does not listen to the audio or generate timed subtitles.
5. Render the clip. The job list shows its current processing stage. Progress follows stages; it is not a precise FFmpeg percentage.
6. In **Clips**, watch the render, edit its title, description and tags, confirm your review, and download the MP4. Review the cut's meaning, framing, captions and credits before publishing it anywhere.

Manual mode performs a real local render. It does not run speech recognition or a model. The sample is already synthetic media and is blocked from YouTube publication through this app.

## Add local AI

AI mode requires the optional Python inference dependencies, a complete local faster-whisper model directory, and a running local Ollama server with a downloaded model. The **Settings** checks report what is missing. Installing the browser app alone does not install these tools or models.

Stop ClipForge first. In the project folder, install the AI extra with the launcher's Python:

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -e ".[ai]"
.\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='models/faster-whisper-small')"
```

```sh
# macOS/Linux
.venv/bin/python -m pip install -e '.[ai]'
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='models/faster-whisper-small')"
```

The download command connects to Hugging Face and writes model files into `models/faster-whisper-small`. Download the complete model, including `tokenizer.json`, before processing private media. ClipForge uses those local files during transcription.

Install [Ollama](https://ollama.com/download). Disable its cloud features on the **Ollama server**, restart it, then download a local model once:

```sh
ollama pull qwen3:4b-instruct
```

Use `OLLAMA_NO_CLOUD=1` in the Ollama server's environment, or merge `{"disable_ollama_cloud":true}` into Ollama's `server.json`, following the [official local-only setup](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features). Setting a variable only in the ClipForge terminal does not change an already-running Ollama desktop server.

Restart ClipForge and enter the speech-model directory and Ollama model name in **Settings**. CPU with `int8` speech computation is the baseline. Start with short recordings and allow time for processing; CPU AI can be slow. The README explains RAM planning, language selection and optional CUDA setup.

Choose **Let AI choose** to use AI mode. It transcribes locally, asks the local model to select grounded intervals, renders timed speech captions, and creates proposed metadata. Always review the resulting cuts and words. Model readiness checks confirm availability, not editorial accuracy.

## Optional YouTube publishing

Downloading a clip needs no Google account. Publishing from ClipForge requires your own YouTube channel, a Google project with the YouTube Data API enabled, a Desktop OAuth client JSON file, and the optional authentication dependencies. Follow the [complete OAuth and recovery guide](upload-notes.md).

Install the extra with `.venv\Scripts\python.exe -m pip install -e ".[youtube]"` on Windows, or `.venv/bin/python -m pip install -e '.[youtube]'` on macOS/Linux. Restart ClipForge afterward.

In **Settings**, configure the Desktop OAuth file path, exact target channel, desired privacy, and enable uploads. Authorize through Google's normal browser consent. In a reviewed clip's publication form, confirm the audience, synthetic-media disclosure, target/privacy and any future schedule before submitting. Scheduled videos need private status and the configured minimum lead time. No clip is published merely by rendering it or enabling the setting.

Submitted clips are kept immutable so a retry cannot silently change an existing upload intention. Check the reported publication state before retrying or attempting another upload. Reconciliation states require the review described in the upload guide. Revoking authorization deletes local owner API data and attempts to revoke consent; it does not delete videos from YouTube.

For a safely resumable transfer, the clip's review panel offers **Resume existing upload** after any required quota wait or reconnection. This reuses the original reviewed artifact, channel, metadata, declarations and schedule. If the transfer needs reconciliation, follow the upload guide before taking further publication actions. Refresh publishing status in Settings to update processing results.

## Where your files live

The default workspace is the project folder. `config.json` holds settings. `data/state.sqlite3` stores the library, jobs and pipeline state; browser uploads go into `data/studio/sources/`, and their job manifests into `data/studio/manifests/`. `data/cache/` stores caches, and `prepared/` contains generated clip artifacts. Credentials, if configured, belong in `secrets/`. The app shows the current workspace path. These locations follow the configured data/cache root if you customize it.

Keep those folders when updating the application. Back up the whole workspace if you need its history and original uploaded files. A local-path import continues to depend on the original file outside the workspace. Model files and videos can use substantial disk space. Downloaded MP4 files are saved wherever your browser normally saves downloads.

For a separate workspace, use an absolute config path:

```powershell
.\Start-ClipForge.bat --config "D:\My ClipForge Workspace\config.json"
```

```sh
bash ./start-clipforge.sh --config "$HOME/My ClipForge Workspace/config.json"
```

A missing config is created with defaults. `paths.root` defaults to the config file's directory; relative media, model and credential paths resolve from that root. Stop jobs before changing settings. Advanced CLI operations continue to use the same configuration and pipeline state.

## Privacy and connections

The browser interface uses files served by the local application, with no hosted fonts, scripts or analytics. The server accepts local requests and protects state changes with an app token and origin checks. The browser is not given OAuth tokens or upload-session URLs.

Manual rendering needs no external service. AI inference uses local speech weights and loopback Ollama. Installing software and downloading models requires internet access; optional OAuth, publish, status-check and revoke actions connect to Google/YouTube. The separate CLI discovery commands connect to YouTube when invoked. Keep OAuth credentials, private recordings, model files and runtime data out of public repositories.

## Troubleshooting

- **Python missing:** install Python 3.11+ and reopen the launcher. On Windows, enable Python on PATH. On Debian/Ubuntu, a matching `python3-venv` package may be needed.
- **Basic setup failed:** read the terminal's error, check your internet connection and folder permissions, then start the launcher again. A broken or outdated `.venv` can be renamed while the app is stopped so the launcher creates another one.
- **FFmpeg missing or unsupported:** set both executable paths in Settings. Use a build with libx264, AAC and libass; installing only a Python package named FFmpeg does not supply the required executables.
- **Port already in use:** stop the other ClipForge terminal, or launch with `--port 8001` and use the new address printed in that terminal.
- **AI not ready:** check the optional AI package, complete local speech-model directory, running local Ollama server and exact downloaded model name. Manual mode remains available when FFmpeg is ready.
- **A job failed or was interrupted:** read its message and fix the missing file/tool or invalid setting before retrying. Keep imported source files in place. Restarting does not resume a partially written render automatically.
- **No automatic timed captions in manual mode:** its optional text is a fixed overlay. Use AI mode after installing local inference tools for speech-based captions.

See [README.md](../README.md) for the full command-line pipeline, architecture, policies and current verification scope.
