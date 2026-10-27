Param(
  [string]$ListenHost = "127.0.0.1",
  [int]$ListenPort = 8000
)

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

if (Test-Path .\.venv\Scripts\Activate.ps1) {
  . .\.venv\Scripts\Activate.ps1
}

python -m uvicorn cliq.web.server:app --host $ListenHost --port $ListenPort --reload --reload-exclude ".venv" --reload-exclude "node_modules" --reload-exclude ".git" --reload-exclude "__pycache__"
