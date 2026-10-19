# Instagram and TikTok publishing

ClipForge uploads approved local MP4 files through the providers' official APIs. Internet access, authorized developer applications and a current access token are required. Saving settings does not authorize an account or publish a clip. Credential files stay on your computer; their contents and secret upload URLs are excluded from browser responses.

## Set up Instagram Reels

Use an Instagram Professional account linked to a Facebook Page, with a Meta app configured for Facebook Login for Business and the required Instagram publishing permissions. Obtain a Page access token through Meta's supported authorization flow. Access for customers outside your app's permitted development roles can require App Review and Advanced Access. This integration uses `graph.facebook.com` and Meta's direct file upload host; an Instagram Login token intended for `graph.instagram.com` is not interchangeable.

Create a private file such as `secrets/instagram.json`:

```json
{
  "access_token": "YOUR_AUTHORIZED_FACEBOOK_PAGE_ACCESS_TOKEN",
  "account_id": "YOUR_NUMERIC_INSTAGRAM_PROFESSIONAL_ACCOUNT_ID",
  "login_type": "facebook_business",
  "expires_at": "2099-01-01T00:00:00Z"
}
```

Replace every placeholder. Set `expires_at` to the token's actual expiry; omit it only when the token has no documented expiry. The example date does not extend a token. Restrict the secrets folder to your operating-system account and keep it outside shared folders and source control.

In Settings, enter the same Instagram account ID, the credential file path and the Graph API version your approved app supports (default `v25.0`), then enable Instagram. Load the account before posting. ClipForge verifies the Page token's linked `instagram_business_account` matches your configured destination. Review the Reel, caption and **Share to feed** choice before confirming publication.

The upload creates one Reel container with `upload_type=resumable`, streams the local video to its returned `rupload.facebook.com` URI, checks processing status, then sends `media_publish` once. It needs no publicly hosted video URL. If processing takes longer than the initial brief check, use **Refresh status**. When ready, **Finish publishing** sends the saved container after another explicit confirmation. The app applies a conservative 300 MiB file cap and a 3-second to 15-minute duration check; the platform can enforce additional specifications and account limits. [Meta's official Reels sample](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md) describes this flow and links the platform requirements.

## Set up TikTok Direct Post

Register a TikTok developer app, enable Content Posting API **Direct Post**, obtain approval for `video.publish`, and obtain that user's authorized access token and open ID. Public distribution depends on TikTok review, audit and eligibility. Unaudited clients have private-account and **Only me** restrictions. A local installation does not grant public-posting approval. [Direct Post setup](https://developers.tiktok.com/docs/en/content-posting-api-get-started).

Create `secrets/tiktok.json`:

```json
{
  "access_token": "YOUR_AUTHORIZED_TIKTOK_ACCESS_TOKEN",
  "open_id": "THE_AUTHORIZED_USER_OPEN_ID",
  "scope": "video.publish",
  "client_audited": false,
  "expires_at": "2099-01-01T00:00:00Z"
}
```

Use the real token expiry, with the same optional-expiry rule as Instagram. Change `client_audited` only when TikTok has actually approved your client; this declaration does not grant approval or bypass provider restrictions. Configure this credential file and enable TikTok in Settings.

Load the account for each new post. ClipForge fetches current creator information, displays the destination nickname, offers the returned audience choices and checks the creator's maximum duration and disabled interactions. Select an audience explicitly. Comments, Duet and Stitch start off and cannot be enabled when the creator has disabled them. The server rechecks these options before initializing an upload. [Creator information reference](https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info).

Edit the caption, disclose your own-brand promotion, paid partnership and AI-generated content accurately, and confirm the linked Music Usage Confirmation. Paid partnership content also requires the Branded Content Policy consent and cannot use **Only me** visibility. Preview the finished video before confirming. Direct Post clients must be intended for authentic creators and a broad audience; TikTok's guidelines identify private account-management utilities and arbitrary cross-platform copying as unacceptable use cases. Approval depends on the actual application and workflow. [TikTok's content sharing guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines).

The server sends the chosen caption (at most 2,200 UTF-16 units), privacy, interactions and disclosures to the Direct Post initializer with `source=FILE_UPLOAD`. Video bytes go sequentially to the returned HTTPS provider upload URL. API Bearer tokens are not sent to that upload URL. [Direct Post API](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post), [file transfer protocol](https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide).

An accepted file can still be processing or undergoing moderation. Use **Refresh status** until the provider confirms completion or reports a failure; a private post may have no public post ID. [TikTok status reference](https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status).

## Saved intents, failures and credentials

Each clip has at most one saved publication intent per platform. Before remote creation, ClipForge saves the approved artifact fingerprint, exact options, target, file checksum and size in the local database. Its reviewed files and metadata become immutable. An existing intent never causes a fresh automatic creation request. Unknown create, upload or final publish outcomes require a status check and, when necessary, manual reconciliation in the platform account. Do not delete local history to force a repost: the original request might have succeeded.

Instagram finish uses only the saved container, caption and destination. It cannot create a replacement container or upload again. A persisted flag prevents replaying a final Instagram publish request after its response is lost. TikTok publishes asynchronously from the original upload; refreshing status does not initialize another post. Interrupted binary transfers are deliberately not replayed automatically.

The credential file contains secrets; the local SQLite database can contain secret, temporary upload URLs and should also be protected. Request errors are reported using safe categories rather than raw provider bodies, tokens or URLs. Only HTTPS upload destinations under the expected provider hosts are accepted, redirects are disabled, and environment proxies are not used.

This version does not implement embedded Instagram/TikTok OAuth, automatic token refresh or provider-side revocation. Renew tokens through your approved provider flow and replace the file. Disable publishing and remove the local credential file to disconnect this installation; revoke the app separately in the provider's account settings to revoke remote access. Removing a credential file does not remove published videos or the saved duplicate-prevention history.

These integrations have mocked protocol tests for file uploads, chunk ranges, account/option validation, immutable snapshots and unknown-outcome protection. No real Instagram or TikTok posting was performed without live authorized credentials. Live app approval, permissions, media acceptance, token renewal and moderation remain provider-side requirements to validate with your approved application.
