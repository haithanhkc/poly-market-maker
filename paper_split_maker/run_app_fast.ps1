# Event-driven combined small_pair paper dashboard.
# Books come from the predict.fun websocket, with REST /v1/markets/orderbooks
# when the socket is down. Decisions run on each book and each BTC tick.
# Ack stays 250 ms and cancel stays 500 ms (config.paper.json). A 100 ms
# clock promotes those timers. An unpaired lot takes the opposite ask when
# average cost plus that ask is at most 0.99.
# Simulated fills only. No orders are sent.
# Ledger: runs\app_fast   Browser: http://127.0.0.1:8767
# The old poll run stays on run_app_combo.ps1 at http://127.0.0.1:8766.
# This script does not read or print .env values. The app loads PREDICT_*
# the same way as the CLI so mainnet series resolve.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$signature = @"
using System;
using System.Runtime.InteropServices;
public static class PaperKeepAwakeFast {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
if (-not ("PaperKeepAwakeFast" -as [type])) {
    Add-Type -TypeDefinition $signature | Out-Null
}
$continuous = [uint32]2147483648
$system = [uint32]1
$away = [uint32]64
[PaperKeepAwakeFast]::SetThreadExecutionState($continuous -bor $system -bor $away) | Out-Null

try {
    $activate = Join-Path $PSScriptRoot "..\.venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        throw "Missing $activate"
    }
    . $activate
    $out = Join-Path $PSScriptRoot "runs\app_fast"
    if (Test-Path (Join-Path $out "state.json")) {
        Write-Host "Resuming $out\state.json"
    }
    python -m paper_split_maker.app `
        --config config.paper.json `
        --out runs\app_fast `
        --port 8767 `
        --fast
    exit $LASTEXITCODE
}
finally {
    [PaperKeepAwakeFast]::SetThreadExecutionState($continuous) | Out-Null
}
