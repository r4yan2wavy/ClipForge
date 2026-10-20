# ClipForge browser app

ClipForge's local server opens a studio in your browser at `http://127.0.0.1:8000/`. Your own computer performs video processing and stores the results. This version runs as a Python application; the launchers prepare its basic environment, while FFmpeg and optional AI tools are separate installations.

## Start and stop

Download the repository ZIP from GitHub and extract all of it into a writable folder. Install Python 3.11 or newer and [FFmpeg with ffprobe](https://ffmpeg.org/download.html). Choose an FFmpeg build with libx264, AAC and libass. Windows users should enable **Add Python to PATH** when installing Python.

- **Windows:** double-click `Start-ClipForge.bat`.
- **macOS/Linux:** open a terminal in the project folder and run `bash ./start-clipforge.sh`.

The first launch creates `.venv` and installs the basic Python dependencies and missing link-import packages from the package index. It needs internet access for those downloads. Later launches reuse that environment and install missing link dependencies when needed. No speech models, Ollama models, FFmpeg binaries or JavaScript runtime are downloaded by the launcher.

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
2. Open **Studio** and add a video. Browser upload copies a file into ClipForge's workspace. Importing an existing local file by its full path registers that file in place; keep it at that path while it is in use. The link option downloads eligible direct HTTPS media or public YouTube/Twitch/Kick videos, or saves a platform reference awaiting your authorized original. **Try a sample** registers the included synthetic test pattern and tones.
3. Choose **Pick a moment** for manual mode, enter a title, and set a start and end inside the source. A manual cut can be 1–180 seconds. Confirm you own the recording or have permission to edit it, and complete its rights/credit information.
4. Choose framing: **center crop** fills the vertical frame and trims the sides; **fit** preserves the whole image with padding; **blur** fits the image over a blurred background. Optional manual text is a static overlay for the entire cut. It does not listen to the audio or generate timed subtitles.
5. Render the clip. The job list shows its current processing stage. Progress follows stages; it is not a precise FFmpeg percentage.
6. In **Clips**, watch the render, edit its title, description and tags, confirm your review, and download the MP4. Review the cut's meaning, framing, captions and credits before publishing it anywhere.

Manual mode performs a real local render. It does not run speech recognition or a model. The sample is already synthetic media and is blocked from platform publication through this app.

## Add a link

Choose **Paste a link** in Studio to add an authorized direct HTTPS video or public YouTube video, Twitch VOD/clip or Kick VOD/clip. The page importer uses yt-dlp locally within this app, with no paid cloud API or subscription. Public Twitch clips use the official download API when broadcaster/editor credentials are configured, otherwise the local page importer. Existing saved references only import when you explicitly choose **Import video**.

The launcher installs missing link-import Python dependencies. An existing environment can install them with `.venv\Scripts\python.exe -m pip install -e ".[links]"` on Windows or `.venv/bin/python -m pip install -e '.[links]'` on macOS/Linux. YouTube also needs Deno 2.3+ or Node.js 22+ on PATH; an existing supported installation is enough. ClipForge detects Deno first, then Node.js, and reports missing prerequisites in readiness checks. It uses the EJS helper supplied by the Python package rather than downloading remote JavaScript components. See the [link setup guide](link-import.md) for installation links and updates.

Instagram/TikTok pages appear under **Linked videos** as **Video file needed**. Choose **Attach a video** to select an authorized library source or upload your original before editing. Failed link imports can also use an attached file. Live streams, channels, playlists, login/cookie-dependent and protected content are excluded. Site changes and access/bot checks can still make public links fail. Check that downloading and using the media is permitted under its rights and the site's terms.

An import downloads the full source onto your PC, with a 10 GiB combined-media limit and a 30-minute overall budget. There is no fixed source-duration maximum; large/slow transfers may hit those limits. FFmpeg may combine separate downloaded audio/video files. Subsequent editing and optional AI also run locally, so there is no paid processing subscription. Your computer needs enough disk space and must remain on until the job finishes.

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

### Repair an existing AI installation

If AI jobs failed with `TypeError` before captions started, an older ClipForge AI installation may have installed PyAV 19 alongside faster-whisper 1.2.1. Those versions are incompatible. ClipForge 0.4.1 constrains PyAV to a compatible version and checks the actual audio decoder in Settings.

Stop ClipForge, update the project files, then run this in the project folder on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ai,links]"
```

On macOS/Linux use `.venv/bin/python -m pip install -e '.[ai,links]'`. Restart ClipForge, save Settings, and check setup. Existing speech models and videos do not need to be downloaded again. Keep one AI job running at a time. New unexpected processing errors report the failed stage and exception category; safe diagnostic locations are stored as `job_failure` events in the local database, without exception messages or credentials.

## Optional YouTube publishing

Downloading a clip needs no Google account. Publishing from ClipForge requires your own YouTube channel, a Google project with the YouTube Data API enabled, a Desktop OAuth client JSON file, and the optional authentication dependencies. Follow the [complete OAuth and recovery guide](upload-notes.md).

Install the extra with `.venv\Scripts\python.exe -m pip install -e ".[youtube]"` on Windows, or `.venv/bin/python -m pip install -e '.[youtube]'` on macOS/Linux. Restart ClipForge afterward.

In **Settings**, configure the Desktop OAuth file path, exact target channel, desired privacy, and enable uploads. Authorize through Google's normal browser consent. In a reviewed clip's publication form, confirm the audience, synthetic-media disclosure, target/privacy and any future schedule before submitting. Scheduled videos need private status and the configured minimum lead time. No clip is published merely by rendering it or enabling the setting.

Submitted clips are kept immutable so a retry cannot silently change an existing upload intention. Check the reported publication state before retrying or attempting another upload. Reconciliation states require the review described in the upload guide. Revoking authorization deletes local owner API data and attempts to revoke consent; it does not delete videos from YouTube.

For a safely resumable transfer, the clip's review panel offers **Resume existing upload** after any required quota wait or reconnection. This reuses the original reviewed artifact, channel, metadata, declarations and schedule. If the transfer needs reconciliation, follow the upload guide before taking further publication actions. Refresh publishing status in Settings to update processing results.

## Optional Instagram and TikTok publishing

Use **Publish to Instagram** or **Publish to TikTok** in a reviewed clip's panel. Each platform has its own account, credentials and confirmation. The app uploads the actual prepared MP4 through the official APIs; you do not need to host the clip at a public URL. Local rendering and downloads work without either integration.

These integrations require external developer setup: obtain the platform's approved app permissions and your account's authorized token, save the token in a private JSON file, then enter its file path and enable that destination in **Settings**. ClipForge does not register these applications, complete their OAuth flows, renew tokens or obtain provider approval automatically. Placeholder files are included for [Instagram](../examples/instagram.credentials.example.json) and [TikTok](../examples/tiktok.credentials.example.json); copy them into your private `secrets/` folder and replace every placeholder before use. Never commit the completed files.

**Instagram Reels:** use the supported Facebook Login for Business flow with a Page access token linked to the target Instagram Professional account. The JSON `account_id` and configured target account ID must match. This is separate from an Instagram Login token. Review the caption and exact destination before confirming the upload. See the [social publishing guide](social-publishing.md) and [Meta's official Reels sample](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md).

**TikTok:** use an authorized `video.publish` token. The form fetches current creator information; check the displayed account, choose one of its permitted privacy settings, and explicitly choose interactions/disclosures and confirm music usage. No privacy option is preselected. Unaudited clients are limited to private accounts and `SELF_ONLY`; public posting requires TikTok's audit. Setting `client_audited` in a JSON file records your application's real status and does not grant approval. TikTok's intended-use guidelines exclude tools limited to private/team account management and copying arbitrary content from other platforms. A public consumer app still needs its own eligible review. [TikTok setup](https://developers.tiktok.com/docs/en/content-posting-api-get-started), [content-sharing requirements](https://developers.tiktok.com/docs/en/content-sharing-guidelines).

Submitting an upload is not proof that a post has finished processing or become visible. Use **Check status** for that destination. An Instagram container that finishes processing can offer **Finish Instagram publishing** to publish the existing prepared container. If a request's outcome is unknown, refresh/reconcile the existing publication; repeated new submissions can create duplicates. Existing publication inputs stay fixed, and social publication does not add unattended retries or scheduled posting. Follow the [platform setup and recovery guide](social-publishing.md) for exact states and supported actions.

To stop local use of Instagram/TikTok credentials, disable the destination and remove its private token file. That local removal does not revoke the provider's authorization or delete a post. Revoke permissions through the provider separately when needed. Keep credential files and backups limited to your OS account; this is local file storage, not an encrypted credential vault.

## Where your files live

The default workspace is the project folder. `config.json` holds settings. `data/state.sqlite3` stores the library, jobs and pipeline state; browser uploads go into `data/studio/sources/`, and their job manifests into `data/studio/manifests/`. `data/cache/` stores caches, and `prepared/` contains generated clip artifacts. Credentials, if configured, belong in `secrets/`. The app shows the current workspace path. These locations follow the configured data/cache root if you customize it.

Keep those folders when updating the application. Back up the whole workspace if you need its history and original uploaded files. A local-path import continues to depend on the original file outside the workspace. Model files and videos can use substantial disk space. Downloaded MP4 files are saved wherever your browser normally saves downloads.

To reclaim space after rendering, choose **Delete source video** on an app-managed downloaded or browser-uploaded source and confirm. Wait until jobs using it finish. This deletes the source file while preserving finished clips and their metadata. The source is recorded as unavailable; import/upload it again to make more clips. The app never deletes an original external file registered by its local path. Deleting a **Linked videos** card removes only the reference, not its source file.

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

Manual rendering needs no external service. AI inference uses local speech weights and loopback Ollama. Installing software and downloading models requires internet access. Explicit direct-link imports connect to the file's host; yt-dlp imports connect to the selected YouTube/Twitch/Kick site and its media hosts; official Twitch imports validate the token and connect to Twitch/download hosts. Optional publishing and status checks connect to the selected YouTube, Instagram or TikTok services, and YouTube authorization/revocation uses Google. Opening an original link contacts that website in your browser. The separate CLI discovery commands connect to YouTube when invoked. Keep credentials, signed source URLs, private recordings, model files and runtime data out of public repositories.

## Troubleshooting

- **Python missing:** install Python 3.11+ and reopen the launcher. On Windows, enable Python on PATH. On Debian/Ubuntu, a matching `python3-venv` package may be needed.
- **Basic setup failed:** read the terminal's error, check your internet connection and folder permissions, then start the launcher again. A broken or outdated `.venv` can be renamed while the app is stopped so the launcher creates another one.
- **FFmpeg missing or unsupported:** set both executable paths in Settings. Use a build with libx264, AAC and libass; installing only a Python package named FFmpeg does not supply the required executables.
- **Port already in use:** stop the other ClipForge terminal, or launch with `--port 8001` and use the new address printed in that terminal.
- **AI not ready:** check the optional AI package, complete local speech-model directory, running local Ollama server and exact downloaded model name. Manual mode remains available when FFmpeg is ready.
- **A job failed or was interrupted:** read its message and fix the missing file/tool or invalid setting before retrying. Keep imported source files in place. Restarting does not resume a partially written render automatically.
- **No automatic timed captions in manual mode:** its optional text is a fixed overlay. Use AI mode after installing local inference tools for speech-based captions.
- **A platform link says it needs a file:** it is a saved reference. Upload/import your authorized original and attach that source. A webpage is not automatically a downloadable video.
- **A YouTube/Twitch/Kick link cannot import:** check link dependencies, FFmpeg/ffprobe and YouTube's Deno/Node.js prerequisite. Update the `links` extra after site changes. Login-required/protected videos, live streams, channels and playlists are excluded; bot/region/access restrictions may also stop a public link. Attach an authorized original when import is unavailable.
- **Official Twitch clip import cannot authorize:** check the configured token's app ID, broadcaster/editor IDs, granted clip-management scope and real expiration; obtain a renewed authorized token externally if needed.
- **Instagram/TikTok rejects publication:** check the private token file, account/app permissions, current provider limits and destination-specific status. Update expired tokens through the provider's OAuth process. Read the [recovery guide](social-publishing.md) before retrying an uncertain submission.

See [README.md](../README.md) for the full command-line pipeline, architecture, policies and current verification scope.
