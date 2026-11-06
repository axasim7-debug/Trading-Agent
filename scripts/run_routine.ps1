# Runs one trading routine headlessly with Claude Code (uses your Claude subscription login, no API key).
#   powershell -ExecutionPolicy Bypass -File scripts\run_routine.ps1 -Routine trade-cycle
param(
    [ValidateSet("trade-cycle", "daily-prep", "daily-review", "weekly-review")]
    [string]$Routine = "trade-cycle",
    [string]$Model = "opus",
    [string]$Context = ""      # set by the watchdog when a price alert wakes the agent
)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force logs | Out-Null

# Skip intraday routines while the FX market is closed (Fri 21:00 UTC -> Sun 21:00 UTC).
$utc = [DateTime]::UtcNow
$closed = ($utc.DayOfWeek -eq "Saturday") -or
          ($utc.DayOfWeek -eq "Sunday" -and $utc.Hour -lt 21) -or
          ($utc.DayOfWeek -eq "Friday" -and $utc.Hour -ge 21)
if ($closed -and $Routine -ne "weekly-review") { exit 0 }

# Never run two routines at once.
$lock = Join-Path $root "logs\routine.lock"
if (Test-Path $lock) {
    if (((Get-Date) - (Get-Item $lock).LastWriteTime).TotalMinutes -lt 50) { exit 0 }
}
Set-Content $lock $PID
# Emergency stop: create a file named STOP in the repository folder.
if (Test-Path (Join-Path $root "STOP")) { Remove-Item $lock; exit 0 }

$stamp = Get-Date -Format "yyyy-MM-dd_HH-mm"
$log = Join-Path $root "logs\$stamp-$Routine.log"
$prompt = "Run the $Routine routine now. Follow .claude/skills/$Routine/SKILL.md and CLAUDE.md exactly."
if ($Context) { $prompt += " You were woken early by the watchdog: $Context. Handle this first." }
try {
    claude -p $prompt --model $Model `
        --allowedTools "mcp__trading" "WebSearch" "WebFetch" "Read" `
        --output-format text *>&1 | Out-File -FilePath $log -Encoding utf8
} finally {
    Remove-Item $lock -ErrorAction SilentlyContinue
}
