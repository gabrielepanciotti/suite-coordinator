# Find the big reclaimable disk consumers on C: (run ad hoc).
$ubuntu = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Filter '*Ubuntu*' -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ChildItem $_.FullName -Recurse -Filter ext4.vhdx -ErrorAction SilentlyContinue } |
    Select-Object -First 1
if ($ubuntu) { '{0,8:N1} GB  Ubuntu vhdx: {1}' -f ($ubuntu.Length / 1GB), $ubuntu.FullName }

$rb = (New-Object -ComObject Shell.Application).Namespace(10).Items() |
    Measure-Object -Property Size -Sum
'{0,8:N1} GB  Recycle Bin' -f ($rb.Sum / 1GB)

foreach ($f in @('C:\hiberfil.sys', 'C:\pagefile.sys')) {
    if (Test-Path $f) {
        '{0,8:N1} GB  {1}' -f ((Get-Item $f -Force).Length / 1GB), $f
    }
}
'{0,8:N1} GB  FREE on C:' -f ((Get-PSDrive C).Free / 1GB)
