# Paper small_pair, 10 BTC 5-minute sessions, Binance lean. No orders.
# Keep the machine awake, then restore the previous execution state.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$signature = @"
using System;
using System.Runtime.InteropServices;
public static class PaperKeepAwake {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
if (-not ("PaperKeepAwake" -as [type])) {
    Add-Type -TypeDefinition $signature | Out-Null
}
$continuous = [uint32]2147483648
$system = [uint32]1
$away = [uint32]64
[PaperKeepAwake]::SetThreadExecutionState($continuous -bor $system -bor $away) | Out-Null

try {
    $activate = Join-Path $PSScriptRoot "..\.venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        throw "Missing $activate"
    }
    . $activate
    python -m paper_split_maker `
        --config config.paper.json `
        --strategy small_pair `
        --series btc-updown-5m `
        --binance-lean `
        --out runs\smallpair_bn10 `
        --max-markets 10 `
        --duration-seconds 3900 `
        --capital 5000 `
        --max-loss-fraction 0.20
    exit $LASTEXITCODE
}
finally {
    [PaperKeepAwake]::SetThreadExecutionState($continuous) | Out-Null
}
