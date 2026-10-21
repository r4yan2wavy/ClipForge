# AI Story Generator

ClipForge has two local workflows: **Studio** turns existing videos into clips; **AI Story Generator** creates an original animated story from an idea. Open the story generator from the left navigation or visit `http://localhost:8000/#stories` while ClipForge is running.

## Make a story

1. Enter an idea, or choose **Surprise me** for a starting point. **Generate 5 ideas** asks your configured local Ollama model for suggestions.
2. Choose a genre, target duration, installed narrator, captions, background music and output quality.
3. Select **Generate video**. The project is saved before ClipForge writes the script, plans its scenes, records narration, animates its characters and exports the video.
4. Preview the completed video and choose **Download MP4**, or **Review & share** to use the existing approval and publishing controls.

Dark Stickman Stories uses reusable character designs and procedural geometric environments. Characters move continuously through each scene, with walking, head turns, arm and object actions, reactions and expressions. Motion beats guide how those actions unfold alongside camera movement, scene transitions and atmospheric effects. The renderer does not need a video diffusion model or a paid service. Videos have no watermark. The target duration is a writing guide; the exported length follows the actual narration so spoken lines are not cut off.

## Local setup

The setup banner checks for the selected Ollama model, rendering tools and installed local narrators. Fix the component identified in the banner, then choose **Check again**. Existing clipping tools keep using their current settings.

The generator reuses your local Ollama model from **Settings → Local AI**. Narration uses available local voices, shown with their provider and language in the narrator menu. Select **Preview voice** to hear a short local sample before generating a full story. Audio and animation are produced on this computer. No project needs to be uploaded to a hosted generation service. ClipForge still requires an explicit review and upload confirmation if you choose to publish to a connected account.

Install the optional renderer from the project folder, using the same Python environment as the app:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[stories]"
```

Windows desktop speech voices such as Microsoft David and Microsoft Zira work without a voice download and provide measured word timestamps. For a neural narrator, open **Manage local voices**. The library shows installed narrators and a catalog of additional Piper voices, including their language, download size, provider, license and source. Select **Download voice** to save a model on this computer, then choose **Use narrator** or preview it. You can cancel a download from its card or the workshop. Completed downloads appear in the narrator selector after the library refreshes. Voice downloads run as background tasks and do not replace your current story edits.

The library checks whether the local Piper engine is available and explains any missing setup. If it is missing, install the optional voice engine using the same environment as ClipForge, then restart the app:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[voices]"
```

You can also install a compatible Piper executable on PATH (or in `tools/piper/`) and put a licensed voice's `.onnx` and `.onnx.json` files together in `models/piper/`. Refresh voices after adding them. Piper narrates short phrases and measures their audio boundaries so captions stay readable and synchronized. It does not invent individual word highlights. eSpeak uses whole-scene phrase captions. Voice files download only when you request them; project text and narration are not sent to a voice service. Review each voice model's license and source before using it in published content.

720p is the faster default for story rendering. Choose 1080p for a larger output. The procedural renderer supports CPU processing; local Ollama chooses acceleration according to its installed runtime. A separate AI image or diffusion model is not required.

## Edit and reuse projects

**Save project** keeps an idea and its settings as a draft even before generation tools are ready. Saved projects appear in **Local project history**. Open a project to edit it; ClipForge asks before discarding unsaved changes when switching projects.

Projects and their persistent character libraries are stored under `data/studio/stories/<project-id>/` with the default paths. Each completed export gets a separate folder under `prepared/`, including the MP4, storyboard, character library and optional SRT captions. Deleting a finished clip removes that export while keeping the saved story project available for another render.

After a storyboard exists, edit each scene's narration, setting, view or backdrop, main action, scene object, camera, lighting or effect. The backdrop can show a nighttime sky, forest, city, desert or another world, including the view through a bedroom window. Expand **Character motion & expressions** for more control. Up to eight beats per scene specify a time from 0–100%, the character, action, target and expression. Characters interpolate between these moments rather than holding still images. With no custom beats, ClipForge plans motion from the scene. Changing **Main action** clears that scene's custom beats so the new action can guide its motion. Editing other fields preserves the saved choreography. **Save storyboard** saves those changes. **Generate video** renders the saved scenes into a new export. Earlier finished exports are kept. **Rewrite script** asks the local model for a fresh script, and **Regenerate scene** asks it to rewrite the selected scene. Save a new project to queue another story while an earlier one is being generated. In **Advanced settings → Videos in batch**, choose 3 or 5 to create separate original stories from the same idea. Each batch story gets its own project in history and runs one at a time in the local queue. Your current storyboard is saved first; batch stories get fresh scripts rather than copies of that storyboard.

Generation progress appears beside the preview and in Studio's workshop. **Cancel generation** stops the active or queued task and keeps the saved project and finished exports. After a failure or restart, reopen the project to inspect the error and retry deliberately. Keep the ClipForge terminal running while jobs are active; closing only the browser tab does not stop them.

## Current scope

This workflow is designed for short, English-language, dark minimalist stickman stories. Its backgrounds and characters are procedural assets with controlled visual consistency. It is not a general cinematic video generator, and quality and story originality still benefit from human review. Verify the narration, captions, pacing and ending before sharing a video.
