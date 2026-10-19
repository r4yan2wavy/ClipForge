# Sourcing, platform checks and editorial constraints

Checked against primary documentation on **October 18, 2026**. Quotas, policies and account-specific permissions can change. The Google Cloud console for your actual project remains authoritative for allocated quota. No API key, OAuth account, real approval, creator permission, or monetization outcome was verified for this starter.

## Discovery implementation

`sourcing.discover(settings, db)` performs metadata discovery; it never downloads media. Enable the YouTube Data API in one Google Cloud project and set the configured environment variable (`YOUTUBE_API_KEY` by default). Restrict the key to this API and appropriate application restrictions. Keep HTTP debug logging disabled: request URLs can contain the key.

The implementation runs channel monitoring at `monitor_every_minutes`, and niche search at `search_every_hours`. Known channels use `channels.list(part=contentDetails,statistics)` to resolve `relatedPlaylists.uploads`, then `playlistItems.list(part=contentDetails)`. It follows `nextPageToken` up to `max_pages`. Previously discovered videos continue to be refreshed after leaving the first uploads page. Search uses `search.list(type=video,order=date,publishedAfter=...,q=...)`. IDs are deduplicated, and `videos.list` retrieves metadata/statistics in groups of at most 50; it does not pass `maxResults` with the `id` filter. Missing IDs are reported as unavailable and their discovery rows, observations and scores are removed.

By default, candidates must be 5 minutes–4 hours long, no more than 30 days old, and match configured language metadata. Live, upcoming and archived live-stream resources are excluded. `defaultAudioLanguage` takes precedence over `defaultLanguage`; missing language is reported under `unknown_language_review`. Language metadata is an imperfect hint, and is not speech detection. Search `relevanceLanguage` is also only a hint. Inspect the source and confirm language through local transcription before editing. Adjust age/duration/language settings to your niche. This is metadata eligibility, not editorial or copyright clearance.

The return value contains candidate IDs/titles and `waiting_media` states, filter counts, unavailable IDs, safe error categories, and request/cache counters. No production job is created until an authorized manifest supplies local media. Preserve raw YouTube attribution/identity when presenting API results. Do not persist full summaries into indefinite log archives; metadata belongs in the expiring discovery tables.

Requests use bounded network timeouts and three attempts for network failures or HTTP 429/5xx. Each attempt reserves budget before the request. HTTP 401/403 stops the current run for quota/auth/permission review; permanent request errors also stop. The implementation does not rotate projects, scrape, or retry forever to evade restrictions. An operator can inspect the Cloud console without exposing request URLs in logs.

## Quota facts checked

The current [official quota calculator](https://developers.google.com/youtube/data/determine_quota_cost), updated October 15, 2026, describes **separate default buckets**: 100 `search.list` calls/day and 100 `videos.insert` calls/day, with each call costing one unit in its respective bucket. Other endpoints share a default 10,000 units/day. Each extra page is another request, and invalid requests still cost quota. Quota resets at midnight Pacific Time. Historical examples saying search always costs 100 general units or upload costs 1,600 units are not this check's current figures. Verify your assigned buckets before use.

ClipForge's `search_daily_budget=20` and general `daily_budget_units=500` are conservative operator choices, not platform limits. These counters cover this local database only; other applications sharing the project also consume quota. `search_call_cost` and `general_read_call_cost` are configurable if documentation changes. Upload budgeting is separate. For one configured channel with two uploads pages and one video batch, the current read estimate is one channel lookup + two playlist requests + one video request = four general units, excluding retries and supplemental lookups.

The [search reference](https://developers.google.com/youtube/data/reference/rest/v3/search/list) documents video-only filtering, dates, language hints, page tokens and 0–50 results per page. Search results are pointers; retrieve full video resources before using metadata. Use slow niche discovery and cheaper uploads monitoring for established channels.

The [channels reference](https://developers.google.com/youtube/data/reference/rest/v3/channels/list) documents the one-unit read and the uploads playlist in `contentDetails`. The [playlistItems reference](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list) documents a one-unit read, page tokens and up to 50 results per page. The [videos reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/list) documents the one-unit read and notes `maxResults`/`pageToken` are not supported alongside `id`. ClipForge explicitly batches ID requests rather than relying on list pagination for those calls.

The [channel resource](https://developers.google.com/youtube/data/reference/rest/v3/channels#statistics.subscriberCount) documents public subscriber counts rounded down to three significant figures, plus `hiddenSubscriberCount`. Ratios using that number are approximate and must not imply an exact audience size.

## Additional metrics are off until the use case is eligible

The [policy guide](https://developers.google.com/youtube/terms/developer-policies-guide), updated September 14, 2026, distinguishes simple API-only arithmetic from additional derived analytics. Its additional-policy section says audited analytics use cases must explicitly apply through the standard quota-extension process and receive approval. Apply under Section 5, Analytics & Reporting, and verify the actual use case is accepted. This starter conservatively gates the entire trend feature, including the simple component ratios, because promoting promising sources from custom comparative signals is an additional analytics use case. That gating choice is an implementation interpretation; it is not a claim that every arithmetic operation always needs separate approval.

The [additional derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy) requires acceptance of the policy amendment for eligible analytics tools and clear distinction between custom analysis and YouTube metrics. Accepted clients may retain certain statistical and derived data for up to 36 calendar months; ordinary metadata such as titles and descriptions still follows the 30-day refresh/deletion rules. ClipForge does **not** take this extended-retention option: it keeps the same short retention limit even when analytics is enabled.

`derived_metrics_enabled=true` never grants approval. `approval_reference` must identify a local evidence-record JSON, with:

```json
{
  "issuer": "YouTube",
  "project_number": "ACTUAL_PROJECT_NUMBER",
  "authorized_use_case": "clipforge_discovery_trends",
  "approved_metrics": ["views_to_subscribers", "recent_velocity", "velocity_lift", "comparable_age"],
  "approval_reference": "ACTUAL_APPROVAL_OR_CASE_REFERENCE",
  "evidence_path": "actual-approval-document.pdf",
  "evidence_sha256": "SHA256_OF_THE_REAL_DOCUMENT",
  "amendment_accepted_at": "ACTUAL_TIME_WITH_UTC_OFFSET",
  "reviewed_by": "ACTUAL_OPERATOR",
  "reviewed_at": "ACTUAL_TIME_WITH_UTC_OFFSET",
  "expires_at": null
}
```

No approval document is provided. Obtain real evidence, verify its issuer, applicable project, analytics scope and any conditions, and manually confirm amendment acceptance before creating this record. The code checks field completeness, scope, dates, existence and hash integrity of the attachment. It cannot contact YouTube to establish authenticity or confirm the API key belongs to the recorded project. A fabricated JSON/document or a correct hash of inapplicable material confers no permission. If evidence is absent/invalid/expired, no custom analytics is computed, and previously stored estimates are deleted. Synthetic test evidence is used only in isolated offline tests.

## Signal behavior for an accepted use case

These are **ClipForge estimates, not YouTube-provided metrics**:

1. Views / visible subscribers: unavailable for hidden, missing or zero counts; suppressed below `min_subscribers` (default 1,000, a tuning choice). Rounded denominators produce an approximate ratio. Do not compare tiny-channel ratios directly with large established audiences.
2. Recent velocity: `(latest_views - previous_views) / actual_elapsed_hours`. Snapshots record the time of an actual API response; cached responses retain their original time and never add another sample. Intervals below `min_observation_minutes` (default 15) are rejected.
3. Velocity lift: recent velocity / `max(previous_velocity, velocity_floor)`, requiring at least three observations with valid elapsed intervals. The floor prevents division by zero but can exaggerate a burst after a quiet period; require meaningful volume as well.
4. Comparable-age ratio: current views / median views from other same-channel videos observed at a similar age. Each other video contributes its closest observed age once. The configurable age tolerance defaults to two hours, and at least five distinct peer videos are required. No current peer total is backdated into historical performance. A zero median or insufficient samples produces no ratio. Slow publishing channels may never have enough history within the 28-day retention window.

Views divided by age would describe lifetime average speed, not current velocity, and is deliberately not used for current-velocity decisions. Unchanged counts may reflect quiet demand or reporting delay, so they yield zero velocity with a warning. Falling/corrected totals invalidate the last three-snapshot velocity calculation and reset confirmation; enough fresh observations must accumulate again. Public aggregate counts do not reveal a different creator's audience retention, satisfaction or watch-time curve.

Defaults require recent velocity >=100 views/hour, lift >=1.5 and comparable-age ratio >=1.5 at three consecutive eligible fresh observations. These are illustrative tuning choices, not universal predictors. A nonqualifying fresh observation, configuration change, or confirmation gap over three hours resets the streak. A failed request or missing statistics cannot advance it; a later valid sample also checks the elapsed gap. A cache hit cannot advance it. Promotion is stored in `discovery_signals`; it never publishes, supplies media rights, or bypasses editorial review. Different niches and publication ages need different thresholds; this starter uses within-channel peer cohorts rather than an assumed universal niche baseline. It makes no virality promise.

## Retention and refresh

[Developer Policies III.E.4](https://developers.google.com/youtube/terms/developer-policies#e.-handling-youtube-data-and-content) permits temporary storage of limited non-authorized API data only as necessary and no longer than 30 calendar days; applicable stored data must be refreshed or deleted. Long retention of some owner-authorized data carries ongoing authorization/deletion checks. This discovery module uses public API-key access and does not assume owner consent. Stored presentations must remain consistent with updated API data.

ClipForge caches responses for 30 minutes by default and periodically refreshes video/channel rows. It purges all discovery response caches, metadata, historical snapshots, estimates and run timestamps older than `retention_days` (default 28, maximum 30). Refreshing a video's current total does not make an old snapshot newly observed; old snapshots are deleted. Missing videos are cleared at their next completed refresh. SQLite secure deletion is enabled and purge checkpoints/truncates its WAL. Keep the database private and expire external backups/exports too; a database row deletion cannot delete independent copies.

Run the worker or `purge()` daily, including when discovery is disabled or credentials fail. If the computer is off for more than the retention window, purge immediately on restart before displaying or using stored API data. The script cannot delete files while the machine is off. This short history constrains comparable-age analysis; it must not be silently extended to make a score work.

## Media and captions

Discovery identifies a source, not a usable file. Obtain original recordings, creator-delivered originals or another appropriately authorized local source. Supply a manifest with rights category, permission/license reference, verified creator credit and editorial contribution. If the file is unavailable, the job remains `waiting_media`. Creator identity, ownership and permission evidence need human verification; a public URL or Creative Commons-looking title is not proof of rights.

[captions.list](https://developers.google.com/youtube/data/reference/rest/v3/captions/list) returns track metadata, not subtitle contents, requires OAuth (`youtube.force-ssl` or partner scope), and costs 50 units. [captions.download](https://developers.google.com/youtube/data/reference/rest/v3/captions/download) costs 200 units, requires those supported scopes and permission to edit the video. A visible caption track or a discovery API key therefore does not imply permission to download captions. The starter implements creator-provided local transcripts/subtitles and local transcription; an authorized caption API adapter is a future integration.

[yt-dlp](https://github.com/yt-dlp/yt-dlp) can technically retrieve media and subtitles from supported sites, with changing extractor dependencies and site compatibility. [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) can retrieve available manual or generated subtitles without a Data API key, and documents availability/access-block limitations. These are technical capabilities, not a grant of platform or copyright permission. Neither is included or invoked by this starter. Scraping or downloading must not become a fallback for exhausted quota, missing caption authorization, access blocks or platform restrictions.

## Shorts, music and editorial contribution

The current [three-minute Shorts help page](https://support.google.com/youtube/answer/15424877) classifies square/vertical videos of up to three minutes uploaded on/after October 15, 2024 as Shorts for standard channels. Official Artist Channels have a December 8, 2025 cutoff. A Short longer than one minute with an active copyright claim is blocked globally under the documented rule. The starter's 20–60 second default is an editorial choice, not YouTube's maximum Shorts duration. Classification alone does not establish monetization or copyright clearance.

[Channel monetization policies](https://support.google.com/youtube/answer/1311392) assess originality/authenticity across a channel. Reused content can fail monetization even when the creator gave permission and no copyright claim appears. Meaningful original commentary, critical review, demonstrations, analysis or a developed narrative can provide an editorial contribution; minimal repackaging is insufficient. Cropping, captions, a music bed, borders or logos do not guarantee eligibility. The starter records and requires an operator-approved contribution statement; it cannot verify that statement's substance or invent a cosmetic compliance score. It also does not guarantee that an automated or repetitive series will satisfy the current policies.

[YouTube's fair-use explanation](https://support.google.com/youtube/answer/9783148) notes that fair use depends on facts and is ultimately determined by courts. Credit/disclaimers or adding some original material do not automatically make an excerpt fair use. Obtain appropriate rights and assess the actual edit separately from monetization eligibility.

For the preapproved local music catalog, record license source, permitted uses and exact required attribution, and check the intended platform, commercial use and modification permissions. The [Audio Library help page](https://support.google.com/youtube/answer/3376882) describes downloading through YouTube Studio and attribution where required; it distinguishes that library from other purported royalty-free sources. The starter uses local catalog files and has no undocumented Audio Library download API. A track's marketing label alone does not establish rights.

## Verification scope

The included tests use synthetic counts, documents and mocked HTTP. They check actual observation intervals, corrected counters, hidden/zero/small subscriber handling, age-matched distinct-peer baselines, playlist monitoring, pagination, 50-ID batching, missing videos, request-budget separation, redacted errors, retention, approval-record integrity, filtering, and confirmation that only new observations advance a trend. See the top-level verification report for the checks actually executed. No live YouTube discovery, real analytics approval, caption access, OAuth authorization or publishing was performed by this module's tests.
