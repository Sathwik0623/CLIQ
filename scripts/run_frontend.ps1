Param(
  [string]$NodeDir = "C:\Program Files\nodejs"
)

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

if (Test-Path $NodeDir) {
  $env:Path = "$NodeDir;$env:Path"
}

Set-Location (Join-Path $PSScriptRoot "..\frontend")

& npm.cmd install
& npm.cmd run dev