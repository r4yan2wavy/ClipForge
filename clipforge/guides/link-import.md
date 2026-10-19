# Adding a source link

The studio can keep a source's original link alongside the media you edit. Some links can supply the video directly; others need an authorized original file attached. Local processing begins only when there is an actual, validated video file.

| Link type | What ClipForge does | What you supply |
|---|---|---|
| Direct HTTPS video file | Downloads the bytes into the local workspace and probes the result | A public HTTPS file URL you are authorized to download and edit |
| Twitch clip | Requests an official download URL, then downloads and validates the media | A supported clip link and the broadcaster/editor credentials described below |
| Supported platform page: YouTube, Twitch, Kick, Instagram or TikTok | Saves a reference awaiting a source file | Your own recording, creator-delivered original, or another authorized local copy |

This feature does not extract video streams from arbitrary platform pages. It does not use a universal scraper, browser cookies, login bypasses or third-party downloader services. Permission to view a post and permission to edit or republish its contents must be established separately.

## Add and attach

1. In **Studio**, choose **Paste a link**, enter the **Video URL**, confirm you are authorized to use the media, and choose **Add link**.
2. For a direct video or configured Twitch clip, start the import and wait for its background job. Once validated, the downloaded video appears in the source library and can be used for a manual or AI clip.
3. A platform page appears under **Linked videos** as **Video file needed**. Choose **Attach a video** to select an existing library source or **Upload a new video file**. You can also add the authorized original through normal local-path import before attaching it.
4. When the card shows **Video attached**, choose **Create clip**, select your cut or local AI mode, and complete the normal rights, credits and editorial review.

A reference alone has no editable media, generated preview or completed clip. Attaching a file associates the supplied recording with the reference; it does not verify that the recording matches the linked post or that your usage rights are valid. **Open original** opens the external website in your browser, which makes a separate connection to that website.

## Direct video files

Use a direct file link provided by you or the media owner. It must use HTTPS, have a supported video extension in its URL path (for example `.mp4`, `.mov` or `.webm`), and lead to supported video bytes. A webpage, streaming playlist or a share page that requires interactive login is not a direct video file. For private content, downloading the authorized original yourself and uploading/importing it locally is the supported path.

Imports accept at most **10 GiB** and stop after **30 minutes**. The downloader validates public destinations, rechecks redirects, pins resolved destinations for its connections, and blocks loopback/private-network addresses, embedded URL credentials and unsupported platform CDN bypass links. An HTTPS address on your home server or intranet therefore will not import through this feature; use a local file instead. The downloaded result must pass FFprobe validation before appearing as a ready source.

The app does not forward browser cookies, arbitrary authorization headers or environment HTTP proxies to the source host. Failed, oversized or interrupted transfers are cleaned up. A download that fails can be attempted again after correcting its URL or connection; this does not resume a partially received media file.

An expiring signed link can work while valid, but its query may contain a secret. The original request URL stays in private local state for the import; the public library representation omits its query. Treat your workspace database and backups as private. Obtain a fresh URL if it expires rather than trying to circumvent its access controls.

## Twitch clips

ClipForge supports authorized Twitch **clip** downloads through `GET /helix/clips/downloads`. Twitch requires an eligible broadcaster/editor authorization with `channel:manage:clips` or `editor:manage:clips`. The `editor_id` must match the user represented by the token; when using the broadcaster's own user token, set `editor_id` and `broadcaster_id` to that same broadcaster ID. The client ID must belong to the application that issued the token. Returned download URLs expire. [Official Get Clips Download reference](https://dev.twitch.tv/docs/api/reference/#get-clips-download).

Register your own Twitch application and obtain the appropriate **user access token** through Twitch's approved OAuth process. ClipForge does not register an application, run a Twitch sign-in flow or renew tokens for you. Its importer validates the supplied token for each explicit import before requesting clip metadata/download access. There is no continuously connected Twitch session or background Twitch polling. [Twitch OAuth](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/), [token validation](https://dev.twitch.tv/docs/authentication/validate-tokens/).

Copy the placeholder [credential example](https://github.com/r4yan2wavy/ClipForge/blob/main/examples/twitch.credentials.example.json) to the private workspace path `secrets/twitch.json`, then replace its values:

```json
{
  "client_id": "YOUR_REGISTERED_CLIENT_ID",
  "access_token": "YOUR_AUTHORIZED_USER_ACCESS_TOKEN",
  "editor_id": "TOKEN_USER_ID",
  "broadcaster_id": "BROADCASTER_USER_ID"
}
```

An optional `expires_at` field accepts a timezone-aware ISO UTC expiration, such as `2026-12-01T12:00:00Z`; use your token's actual expiry. Restrict the file to your operating-system account. In **Settings**, set the Twitch credentials-file path if you use another location. Paths resolve from the active workspace's root, so a separate `--config` workspace needs its own private credential file.

Use a Twitch clip permalink, such as `https://clips.twitch.tv/CLIP_SLUG` or `https://www.twitch.tv/CHANNEL/clip/CLIP_SLUG`. In **Settings → Twitch clip imports**, set **Private credentials file** to the JSON path. Existing VOD, live-channel and other Twitch pages require an authorized local original instead of this clip-download endpoint. If credentials are missing, expired, invalid or lack access to that broadcaster, the app reports the setup/access problem; replace or renew the token through Twitch's OAuth process and try the import again.

Deleting the local token file prevents subsequent local use; it does not revoke a token at Twitch. Revoke the authorization through Twitch's account connections or approved revocation flow when you no longer need it. API access also does not establish the copyright/license rights for the media you plan to edit.

## Publishing the result

After import, all sources follow the same local clip, review and download workflow. A source URL does not authorize posting it elsewhere. For your own eligible content, configure the destination and confirm each publication as described in the [browser guide](https://github.com/r4yan2wavy/ClipForge/blob/main/docs/local-app.md), [YouTube guide](https://github.com/r4yan2wavy/ClipForge/blob/main/docs/upload-notes.md) and [Instagram/TikTok guide](https://github.com/r4yan2wavy/ClipForge/blob/main/docs/social-publishing.md).

The source-link and Twitch integration documentation was checked against primary API sources on **October 19, 2026**. This describes the implemented flow and setup requirements; it does not claim a live account download test or guarantee that Twitch grants access to a particular clip.
