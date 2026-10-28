# Show which process owns the listeners on the given ports (default: site ports).
param([int[]]$Ports = @(3010, 3012, 3014, 3015, 3016, 8010, 8012, 8014))
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $Ports -contains $_.LocalPort } |
    Sort-Object LocalPort, OwningProcess -Unique |
    ForEach-Object {
        $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        '{0,6}  pid={1,-7} {2,-22} started={3}' -f $_.LocalPort, $_.OwningProcess, $p.Name, $p.StartTime
    }
