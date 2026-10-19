# Synthetic rendering preview

`preview.mp4` was produced by the actual command-line preparation workflow using
the default 1080×1920, 30 fps, H.264/AAC, caption and music settings.

It contains a generated test pattern and two test tones. Its supplied captions,
selection and metadata are synthetic fixtures, **not speech recognition or local
LLM results**. No real source footage, creator identity or licensed song is used.
Do not publish this testing fixture as an editorial clip.

The clip spans original source time 0.4–20.4 seconds. `captions.srt` and
`captions.ass` use the corresponding 0–20 second clip timeline. The actual
FFmpeg output passed checksums, dimensions and complete decoding. Repeating the
preparation command reused its existing artifact.

Recreate it using `examples/make_demo.py` and the commands in the project README.
