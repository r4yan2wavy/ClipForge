# Third-party components

The [ClipForge Use-Only License](LICENSE) applies only to rights in ClipForge
that its licensor owns or can license. Separate third-party rights remain in
effect. Dependencies, external executables, model weights and fonts are installed
separately; they are not bundled in this repository.

| Component | Role | Upstream license/terms source |
|---|---|---|
| Python | Runtime | [Python license](https://docs.python.org/3/license.html) |
| Pydantic | Validation | [Upstream license](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| Requests | HTTP | [Upstream license](https://github.com/psf/requests/blob/main/LICENSE) |
| yt-dlp | Optional local page-link imports | [Unlicense and distribution notes](https://github.com/yt-dlp/yt-dlp#licensing) |
| yt-dlp-ejs | YouTube JavaScript helper installed by the `links` extra | [Upstream license and third-party notices](https://github.com/yt-dlp/ejs) |
| Deno or Node.js | Separately installed JavaScript runtime for YouTube imports | [Deno license](https://github.com/denoland/deno/blob/main/LICENSE), [Node.js license and bundled notices](https://github.com/nodejs/node/blob/main/LICENSE) |
| FileLock | Local process lock | [Upstream license](https://github.com/tox-dev/filelock/blob/main/LICENSE) |
| tzdata | Timezone data | [Upstream license information](https://github.com/python/tzdata/blob/main/LICENSE) |
| FFmpeg / ffprobe | Media processing | [Licensing and build considerations](https://ffmpeg.org/legal.html) |
| faster-whisper | Transcription | [Upstream license](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) |
| Whisper | Speech-model family | [Upstream license](https://github.com/openai/whisper/blob/main/LICENSE) |
| Ollama | Local model server | [Upstream license](https://github.com/ollama/ollama/blob/main/LICENSE) |
| Qwen3 local model | Suggested LLM weights | [Suggested model and its license](https://ollama.com/library/qwen3:4b-instruct) |
| google-auth | OAuth credentials | [Upstream license](https://github.com/googleapis/google-auth-library-python/blob/main/LICENSE) |
| google-auth-oauthlib | Interactive OAuth setup | [Upstream license](https://github.com/googleapis/google-auth-library-python-oauthlib/blob/main/LICENSE) |
| pytest | Development tests | [Upstream license](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |

Check the license shipped with the exact version/build you install, including
transitive dependencies and the specific model weights you choose. FFmpeg
build options can change the applicable license. Fonts retain their own terms.

YouTube and Google APIs are services subject to their own terms and policies,
linked in the project guide and policy notes. Local source recordings, creator
subtitles, music and attribution evidence remain the operator's responsibility.

The optional `links` extra installs the yt-dlp Python distribution through pip;
ClipForge does not bundle a standalone yt-dlp executable. Upstream identifies its
PyPI source and wheel distributions as Unlicense. Some standalone executable
bundles include GPLv3+ components and have different distribution requirements;
those binary bundles are not installed or redistributed here. The separately
installed EJS helper and its bundled components retain their own notices, as do
all transitive dependencies. Check the exact installed distributions.

Technical support for a page link grants no download or reuse rights;
[YouTube's terms](https://www.youtube.com/static?template=terms) and the media's
rights still apply.

The `demo/` preview contains a generated test pattern, tones and synthetic
caption fixtures. It contains no creator footage, downloaded song or model
weights. Its purpose and production method are documented in `demo/README.md`.
