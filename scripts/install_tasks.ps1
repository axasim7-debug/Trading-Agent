# Schedules the routines with Windows Task Scheduler (times are your PC's LOCAL time - adjust them).
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
# Remove later: open Task Scheduler and delete the "TradingAgent" folder tasks, or run e.g.  schtasks /Delete /TN "TradingAgent\TradeCycle" /F
param(
    [string]$DailyPrep = "09:30",     # ~30-60 min before the London open in your time zone
    [string]$DailyReview = "00:15",   # after the New York close
    [string]$WeeklyReview = "14:00",  # Saturday
    [int]$CycleEveryHours = 1
)
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "scripts\run_routine.ps1"
function Add-Task($name, $routine, $schedule) {
    $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Routine $routine"
    schtasks /Create /F /TN "TradingAgent\$name" /TR $action @schedule | Out-Null
    Write-Host "scheduled $name"
}
# Hourly cycle, 2 minutes after the hour so the H1 bar has closed.
Add-Task "TradeCycle" "trade-cycle" @("/SC", "HOURLY", "/MO", "$CycleEveryHours", "/ST", "00:02")
Add-Task "DailyPrep" "daily-prep" @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", $DailyPrep)
Add-Task "DailyReview" "daily-review" @("/SC", "WEEKLY", "/D", "TUE,WED,THU,FRI,SAT", "/ST", $DailyReview)
Add-Task "WeeklyReview" "weekly-review" @("/SC", "WEEKLY", "/D", "SAT", "/ST", $WeeklyReview)
Write-Host "Done. The PC must be on, MT5 open and logged in to the demo account."
