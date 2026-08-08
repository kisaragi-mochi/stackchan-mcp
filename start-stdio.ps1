# Temporary Windows runner. On Linux use the provided bash scripts instead.
#
# Loads gateway/.env into the process environment and starts the stdio MCP
# server. The gateway also calls load_dotenv() itself, but doing it here keeps
# the variables visible to `uv` and to any child process it spawns.

$ErrorActionPreference = 'Stop'

$RepoRoot   = $PSScriptRoot
$GatewayDir = Join-Path $RepoRoot 'gateway'
$EnvFile    = Join-Path $GatewayDir '.env'

if (-not (Test-Path $EnvFile)) {
    throw "No .env found at $EnvFile. Copy gateway/.env.example to gateway/.env and fill it in."
}

# Parse KEY=value / KEY="value" lines; skip comments, blanks, and empty values
# so an unset placeholder never shadows a real default.
foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding utf8) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }

    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }

    $key = $trimmed.Substring(0, $split).Trim()
    $val = $trimmed.Substring($split + 1).Trim()

    # Strip one layer of matching surrounding quotes.
    if ($val.Length -ge 2 -and
        (($val.StartsWith('"') -and $val.EndsWith('"')) -or
         ($val.StartsWith("'") -and $val.EndsWith("'")))) {
        $val = $val.Substring(1, $val.Length - 2)
    }

    if ($key -eq '' -or $val -eq '') { continue }

    Set-Item -Path "env:$key" -Value $val
    Write-Host "loaded $key"
}

Set-Location -LiteralPath $GatewayDir
uv run python -m stackchan_mcp
