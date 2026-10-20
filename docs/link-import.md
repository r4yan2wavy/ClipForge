# Adding a source link

The studio can keep a source's original link alongside the media you edit. Some links can supply the video directly; others need an authorized original file attached. Local processing begins only when there is an actual, validated video file.

| Link type | What ClipForge does | What you supply |
|---|---|---|
| Direct HTTPS video file | Downloads the bytes into the local workspace and probes the result | A public HTTPS file URL you are authorized to download and edit |
| Public YouTube video, Twitch VOD/clip or Kick VOD/clip | Uses yt-dlp locally to download a single source video and merges separate audio/video files if needed | A compatible public link you are permitted to download and edit, link-import dependencies, and working FFmpeg/ffprobe; YouTube also needs Deno or Node.js |
| Twitch clip with configured credentials | Requests an official download URL, then downloads and validates the media | A supported clip link and the broadcaster/editor credentials described below |
| Instagram or TikTok page | Saves a reference awaiting a source file | Your own recording, creator-delivered original, or another authorized local copy |

The page importer uses the free yt-dlp Python package inside ClipForge's existing localhost app. It does not use a paid downloader API or a cloud processing service. Downloads and editing run on your own computer; your bandwidth, disk space and processing power are used. Permission to view a post and permission to download, edit or republish its contents must be established separately. Platform terms apply, including [YouTube's permissions and restrictions](https://www.youtube.com/static?template=terms).

## Add and attach

1. In **Studio**, choose **Paste a link**, enter the **Video URL**, confirm you are authorized to use the media, and choose **Add link**.
2. For a direct video or supported public YouTube/Twitch/Kick video link, start the import and wait for its background job. Once validated, the downloaded video appears in the source library and can be used for a manual or AI clip. Downloading alone does not run AI or create clips.
3. Instagram/TikTok pages and other saved references appear under **Linked videos** as **Video file needed**. Choose **Attach a video** to select an existing library source or **Upload a new video file**. You can also add the authorized original through normal local-path import before attaching it. When an import fails, attach your authorized file or use **Retry** after correcting a missing dependency or temporary connection problem.
4. When the card shows **Video attached**, choose **Create clip**, select your cut or local AI mode, and complete the normal rights, credits and editorial review.

A reference alone has no editable media, generated preview or completed clip. Attaching a file associates the supplied recording with the reference; it does not verify that the recording matches the linked post or that your usage rights are valid. **Open original** opens the external website in your browser, which makes a separate connection to that website.

To organize **Linked videos**, choose **Delete** on a card and confirm. This removes only that saved link; source videos, files, clips and their credits remain available. Wait for an active link import to finish before deleting its card.

Saved references from earlier versions remain references until you explicitly choose **Import video** on a compatible link. Updating ClipForge does not start downloads in the background.

## Set up YouTube, Twitch and Kick link imports

The launchers install missing `links` Python dependencies into ClipForge's private environment. This extra installs `yt-dlp[default]>=2025.11.12,<2028`, including the EJS helper used for YouTube. FFmpeg and ffprobe must still be installed and configured in **Settings**. AI setup is optional and is only needed for automatic transcription, timed speech captions and suggested clip moments.

For an existing Python environment, stop ClipForge and install the extra from the project folder:

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -e ".[links]"
```

```sh
# macOS/Linux
.venv/bin/python -m pip install -e '.[links]'
```

YouTube imports also need **Deno 2.3+** or **Node.js 22+** available on PATH. ClipForge detects Deno first, then Node.js, and enables the detected runtime for yt-dlp. If a supported runtime is already installed, no second runtime is needed. Otherwise install [Deno](https://docs.deno.com/runtime/getting_started/installation/) or [Node.js](https://nodejs.org/en/download/), reopen the launcher so it receives the updated PATH, and check link-import readiness. ClipForge does not install the runtime or automatically fetch EJS components from remote sources; it uses the helper installed with the Python package. [yt-dlp JavaScript setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

Paste a **single video** link: a YouTube video, Twitch VOD/clip or Kick VOD/clip. The local engine retrieves the media and FFmpeg combines separate audio/video files when necessary. Channel pages, playlists, live streams and Instagram/TikTok page downloads are excluded. Clips and VODs must be public and available from your connection. ClipForge does not use browser cookies or account login and cannot import content that requires those, DRM-protected media, or media blocked by access restrictions.

Site changes, region restrictions and bot checks can still make a public link fail. Listed support is not a guarantee that every link works. A failed import reports its error; you can attach an authorized original instead. Retry after fixing setup/connectivity or updating the link dependencies, but repeated retries cannot remove site restrictions. For dependency updates, stop the app and rerun the extra installation above with `--upgrade`. Direct-file imports keep their own route, and a Twitch clip uses the official route first when its credentials file is configured. [yt-dlp project](https://github.com/yt-dlp/yt-dlp), [supported-site limitations](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Local files and source removal

The full source is downloaded to the ClipForge workspace on your PC before clips are made. Imports allow at most **10 GiB of combined media downloads** and a **30-minute overall time budget**. Separate audio/video downloads count toward that same limit. A merge may need additional temporary disk space. There is no fixed maximum source duration: a long or high-quality video may exceed the byte or time limit. This is separate from the editor's 1–180-second limit for each finished clip.

Finished clips are independent saved video files. After all jobs using a source finish, choose **Delete source video** and confirm to remove an app-managed downloaded or browser-uploaded source file. Finished clips remain available. The removed source stays recorded as unavailable, so you must import/upload the source again to make new clips from it. This is separate from deleting a saved link card. Files registered through a local-path import outside app-managed storage are not deleted by the app.

## Direct video files

Use a direct file link provided by you or the media owner. It must use HTTPS, have a supported video extension in its URL path (for example `.mp4`, `.mov` or `.webm`), and lead to supported video bytes. A webpage, streaming playlist or a share page that requires interactive login is not a direct video file. For private content, downloading the authorized original yourself and uploading/importing it locally is the supported path.

Imports use the byte/time limits above. The downloader validates public destinations, rechecks redirects, pins resolved destinations for its connections, and blocks loopback/private-network addresses, embedded URL credentials and unsupported platform CDN bypass links. An HTTPS address on your home server or intranet therefore will not import through this feature; use a local file instead. The downloaded result must pass FFprobe validation before appearing as a ready source.

The app does not forward browser cookies, arbitrary authorization headers or environment HTTP proxies to the source host. Failed, oversized or interrupted transfers are cleaned up. A download that fails can be attempted again after correcting its URL or connection; this does not resume a partially received media file.

An expiring signed link can work while valid, but its query may contain a secret. The original request URL stays in private local state for the import; the public library representation omits its query. Treat your workspace database and backups as private. Obtain a fresh URL if it expires rather than trying to circumvent its access controls.

## Twitch clips

ClipForge prefers the official Twitch **clip** download route through `GET /helix/clips/downloads` when its credentials file is configured. Without that file, supported public clips use the local yt-dlp route described above. The official endpoint requires an eligible broadcaster/editor authorization with `channel:manage:clips` or `editor:manage:clips`. The `editor_id` must match the user represented by the token; when using the broadcaster's own user token, set `editor_id` and `broadcaster_id` to that same broadcaster ID. The client ID must belong to the application that issued the token. Returned download URLs expire. [Official Get Clips Download reference](https://dev.twitch.tv/docs/api/reference/#get-clips-download).

Register your own Twitch application and obtain the appropriate **user access token** through Twitch's approved OAuth process. ClipForge does not register an application, run a Twitch sign-in flow or renew tokens for you. Its importer validates the supplied token for each explicit import before requesting clip metadata/download access. There is no continuously connected Twitch session or background Twitch polling. [Twitch OAuth](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/), [token validation](https://dev.twitch.tv/docs/authentication/validate-tokens/).

Copy the placeholder [credential example](../examples/twitch.credentials.example.json) to the private workspace path `secrets/twitch.json`, then replace its values:

```json
{
  "client_id": "YOUR_REGISTERED_CLIENT_ID",
  "access_token": "YOUR_AUTHORIZED_USER_ACCESS_TOKEN",
  "editor_id": "TOKEN_USER_ID",
  "broadcaster_id": "BROADCASTER_USER_ID"
}
```

An optional `expires_at` field accepts a timezone-aware ISO UTC expiration, such as `2026-12-01T12:00:00Z`; use your token's actual expiry. Restrict the file to your operating-system account. In **Settings**, set the Twitch credentials-file path if you use another location. Paths resolve from the active workspace's root, so a separate `--config` workspace needs its own private credential file.

Use a Twitch clip permalink, such as `https://clips.twitch.tv/CLIP_SLUG` or `https://www.twitch.tv/CHANNEL/clip/CLIP_SLUG`. In **Settings → Twitch clip imports**, set **Private credentials file** to the JSON path. Public Twitch VODs use yt-dlp rather than this official clip endpoint; live/channel pages are excluded. If a configured token is expired, invalid or lacks access to that broadcaster, the official route reports the setup/access problem; replace or renew the token through Twitch's OAuth process and try the import again.

Deleting the local token file prevents subsequent local use; it does not revoke a token at Twitch. Revoke the authorization through Twitch's account connections or approved revocation flow when you no longer need it. API access also does not establish the copyright/license rights for the media you plan to edit.

## Publishing the result

After import, all sources follow the same local clip, review and download workflow. A source URL does not authorize posting it elsewhere. For your own eligible content, configure the destination and confirm each publication as described in the [browser guide](local-app.md), [YouTube guide](upload-notes.md) and [Instagram/TikTok guide](social-publishing.md).

The source-link and Twitch integration documentation was checked against primary API sources on **October 19, 2026**. This describes the implemented flow and setup requirements; it does not claim a live account download test or guarantee that Twitch grants access to a particular clip.
