# Paper dashboard for the combined small_pair run (run B), the slow poll.
# Simulated fills against the live predict.fun book. No orders are sent.
# Ledger: runs\app_combo  (separate from runs\smallpair_combo100).
# Browser: http://127.0.0.1:8766
# The event-driven version is run_app_fast.ps1 on port 8767.
#
# Run this again after a reboot. It resumes runs\app_combo\state.json when
# the capital is still 100 and the loss fraction is still 0.99.
# BOT OFF cancels resting paper bids and lets the open position settle.
# BOT ON quotes again. Reset ledger on the page wipes this folder's ledger
# only after a confirm. It does not read or print .env.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$signature = @"
using System;
using System.Runtime.InteropServices;
public static class PaperKeepAwakeApp {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
if (-not ("PaperKeepAwakeApp" -as [type])) {
    Add-Type -TypeDefinition $signature | Out-Null
}
$continuous = [uint32]2147483648
$system = [uint32]1
$away = [uint32]64
[PaperKeepAwakeApp]::SetThreadExecutionState($continuous -bor $system -bor $away) | Out-Null

try {
    $activate = Join-Path $PSScriptRoot "..\.venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        throw "Missing $activate"
    }
    . $activate
    $out = Join-Path $PSScriptRoot "runs\app_combo"
    if (Test-Path (Join-Path $out "state.json")) {
        Write-Host "Resuming $out\state.json"
    }
    python -m paper_split_maker.app `
        --config config.paper.json `
        --out runs\app_combo `
        --port 8766 `
        --no-fast
    exit $LASTEXITCODE
}
finally {
    [PaperKeepAwakeApp]::SetThreadExecutionState($continuous) | Out-Null
}
