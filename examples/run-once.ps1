# Edit these two ABSOLUTE paths to your installed project and virtual environment.
$clipProject = 'C:\ClipForge'
$clipPython = 'C:\ClipForge\.venv\Scripts\python.exe'
$env:OLLAMA_NO_CLOUD = '1' # Also set on/restart the separately running Ollama SERVER.
# Set YOUTUBE_API_KEY in the scheduler account's environment or securely here.
Set-Location -LiteralPath $clipProject
& $clipPython -m clipforge --config "$clipProject\config.json" run --discovery
exit $LASTEXITCODE
