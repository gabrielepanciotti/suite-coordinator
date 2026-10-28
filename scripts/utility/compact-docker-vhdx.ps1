# Compact the Docker Desktop WSL data disk to give freed VM space back to C:.
# Stops Docker Desktop, compacts via diskpart (Windows Home: no Optimize-VHD),
# restarts Docker Desktop. Containers with restart policies come back on their own.
$vhdx = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"
if (-not (Test-Path $vhdx)) { throw "vhdx not found: $vhdx" }

'{0:N1} GB before' -f ((Get-Item $vhdx).Length / 1GB)

Stop-Process -Name 'Docker Desktop' -Force -ErrorAction SilentlyContinue
Start-Sleep 5
wsl --shutdown
Start-Sleep 5

$script = @"
select vdisk file="$vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
"@
$tmp = Join-Path $env:TEMP 'compact-docker.dpt'
$script | Set-Content $tmp -Encoding ascii
diskpart /s $tmp | Select-Object -Last 3
Remove-Item $tmp

'{0:N1} GB after' -f ((Get-Item $vhdx).Length / 1GB)
Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
'docker desktop restarting'
