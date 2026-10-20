$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start.cmd') @args
exit $LASTEXITCODE
