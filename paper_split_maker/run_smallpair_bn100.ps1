# Open-ended paper small_pair with the Binance filter. Capital 100. No orders.
# The run continues until cash cannot fund another session, you press Ctrl+C,
# or you create runs\smallpair_bn100\STOP.
#
# After a reboot, run this script again. It resumes runs\smallpair_bn100\state.json
# when --capital 100 and --max-loss-fraction 0.99 are unchanged. A different
# bankroll prints the locked capital and floor and keeps the saved ledger.
# If state.json cannot be read, leave the directory in place and pick a new
# --out. Do not delete state.json to change the bankroll.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$signature = @"
using System;
using System.Runtime.InteropServices;
public static class PaperKeepAwake100 {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
if (-not ("PaperKeepAwake100" -as [type])) {
    Add-Type -TypeDefinition $signature | Out-Null
}
$continuous = [uint32]2147483648
$system = [uint32]1
$away = [uint32]64
[PaperKeepAwake100]::SetThreadExecutionState($continuous -bor $system -bor $away) | Out-Null

try {
    $activate = Join-Path $PSScriptRoot "..\.venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        throw "Missing $activate"
    }
    . $activate
    $out = Join-Path $PSScriptRoot "runs\smallpair_bn100"
    if (Test-Path (Join-Path $out "state.json")) {
        Write-Host "Resuming $out\state.json"
    }
    python -m paper_split_maker `
        --config config.paper.json `
        --strategy small_pair `
        --series btc-updown-5m `
        --binance-lean `
        --out runs\smallpair_bn100 `
        --max-markets 0 `
        --duration-seconds 0 `
        --capital 100 `
        --max-loss-fraction 0.99
    exit $LASTEXITCODE
}
finally {
    [PaperKeepAwake100]::SetThreadExecutionState($continuous) | Out-Null
}
