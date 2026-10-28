# Quick disk-usage probe for the big cache folders on C: (run ad hoc).
$paths = @(
    "$env:LOCALAPPDATA\npm-cache",
    "$env:LOCALAPPDATA\pip\Cache",
    "$env:LOCALAPPDATA\Temp",
    "$env:LOCALAPPDATA\Docker\wsl"
)
foreach ($p in $paths) {
    if (Test-Path $p) {
        $s = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum / 1GB
        '{0,8:N1} GB  {1}' -f $s, $p
    }
}
$d = Get-PSDrive C
'{0,8:N1} GB  FREE on C:' -f ($d.Free / 1GB)
