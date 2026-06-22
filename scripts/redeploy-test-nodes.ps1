# Redeploy the freshly-built NexusGrid.exe into the two local test nodes
# (node-C on :8000, node-D on :8001) and relaunch them.
#
#   pwsh -NoProfile -File scripts/redeploy-test-nodes.ps1
#
# Behavior:
#   1. Kill any running NexusGrid.exe / .nexus_cloudflared.exe whose
#      executable lives inside node-C or node-D.
#   2. Prompt for 3 seconds: press [k] to keep state (preserves DB +
#      token + relay config so the node remembers its name and
#      auto-starts the configured relay) or [c] to clean-wipe (fresh
#      DB + fresh token, forces onboarding to reappear). No input in
#      3 seconds = clean-wipe.
#   3. Either replace just NexusGrid.exe (keep) or wipe the whole node
#      dir then copy the fresh exe in (clean).
#   4. Launch each node in a detached window on its assigned port.
#
# An optional `-Choice` param lets non-interactive callers skip the
# prompt — set to "keep" or "clean".

param(
    [ValidateSet('prompt', 'keep', 'clean')]
    [string]$Choice = 'prompt'
)

function Read-ChoiceWithTimeout {
    # 3-second window for the user to press k (keep) or c (clean).
    # Defaults to clean. Returns 'keep' or 'clean'. Falls back to
    # 'clean' if the console doesn't support raw key reads (e.g. when
    # this script runs under a tool with no attached TTY).
    Write-Host -NoNewline "Press [k] to keep state or [c] to clean-wipe (default clean in 3s): "
    $deadline = (Get-Date).AddSeconds(3)
    try {
        while ((Get-Date) -lt $deadline) {
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true)
                $ch = $key.KeyChar.ToString().ToLower()
                Write-Host $ch
                if ($ch -eq 'k') { return 'keep' }
                return 'clean'
            }
            Start-Sleep -Milliseconds 50
        }
        Write-Host '(timeout - clean)'
    } catch {
        Write-Host '(no TTY - clean)'
    }
    return 'clean'
}

$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$exeSrc   = Join-Path $repoRoot 'dist\NexusGrid.exe'
$nodes    = @(
    @{ Dir = Join-Path $repoRoot 'node-C'; Port = 8000 },
    @{ Dir = Join-Path $repoRoot 'node-D'; Port = 8001 }
)

if (-not (Test-Path $exeSrc)) {
    throw "Source exe not found at $exeSrc. Run the PyInstaller build first."
}

Write-Host "[1/4] Stopping running NexusGrid + cloudflared processes in node-C / node-D..."

function Get-ProcsForNodes {
    param([string[]]$Names, [object[]]$Nodes)
    $found = @()
    foreach ($name in $Names) {
        $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            $path = $null
            try { $path = $p.Path } catch {}
            if (-not $path) { continue }
            foreach ($node in $Nodes) {
                if ($path -like (Join-Path $node.Dir '*')) {
                    $found += [PSCustomObject]@{ Pid = $p.Id; Path = $path; Process = $p }
                    break
                }
            }
        }
    }
    return $found
}

# NexusGrid spawns .nexus_cloudflared.exe as a sidecar tunnel. The parent
# kill doesn't propagate, so we collect both names explicitly. Anything
# whose Path lives inside one of our node dirs is fair game.
$procNames = @('NexusGrid', '.nexus_cloudflared', 'nexus_cloudflared', 'cloudflared')
$victims = Get-ProcsForNodes -Names $procNames -Nodes $nodes
foreach ($v in $victims) {
    Write-Host "  killing PID $($v.Pid) ($($v.Path))"
    try {
        Stop-Process -Id $v.Pid -Force -ErrorAction Stop
    } catch {
        Write-Warning "  Stop-Process failed for PID $($v.Pid): $_"
    }
}

# Wait until the processes are actually gone (Stop-Process returns before
# the OS finalizes handle release). Up to 5s, with a short backoff.
$deadline = (Get-Date).AddSeconds(5)
while ((Get-Date) -lt $deadline) {
    $still = Get-ProcsForNodes -Names $procNames -Nodes $nodes
    if ($still.Count -eq 0) { break }
    Start-Sleep -Milliseconds 250
}

function Remove-WithRetry {
    param([string]$Path)
    # Same retry strategy the old wipe-all path used: file handles on the
    # just-killed exes can linger for ~1s.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Milliseconds (300 * $attempt)
        }
    }
}

if ($Choice -eq 'prompt') {
    $Choice = Read-ChoiceWithTimeout
}

if ($Choice -eq 'clean') {
    Write-Host "[2/4] Clean-wipe: nuking node dirs (fresh DB + fresh token)..."
    foreach ($node in $nodes) {
        if (Test-Path $node.Dir) {
            Get-ChildItem -LiteralPath $node.Dir -Force | ForEach-Object {
                try { Remove-WithRetry -Path $_.FullName }
                catch { Write-Warning "  Could not delete $($_.FullName): $_" }
            }
            Write-Host "  cleaned $($node.Dir)"
        } else {
            New-Item -ItemType Directory -Path $node.Dir | Out-Null
            Write-Host "  created $($node.Dir)"
        }
    }
} else {
    Write-Host "[2/4] Keep: preserving node state, only the NexusGrid.exe will be replaced..."
    foreach ($node in $nodes) {
        if (Test-Path $node.Dir) {
            $oldExe = Join-Path $node.Dir 'NexusGrid.exe'
            if (Test-Path $oldExe) {
                try {
                    Remove-WithRetry -Path $oldExe
                    Write-Host "  removed old $oldExe"
                } catch {
                    Write-Warning "  Could not remove old exe at $oldExe (file lock?): $_"
                }
            }
        } else {
            New-Item -ItemType Directory -Path $node.Dir | Out-Null
            Write-Host "  created $($node.Dir)"
        }
    }
}

Write-Host "[3/4] Copying fresh exe into node dirs..."
foreach ($node in $nodes) {
    Copy-Item -LiteralPath $exeSrc -Destination $node.Dir -Force
    Write-Host "  -> $($node.Dir)\NexusGrid.exe"
}

Write-Host "[4/4] Launching nodes..."
foreach ($node in $nodes) {
    $exe = Join-Path $node.Dir 'NexusGrid.exe'
    Write-Host "  launching $exe --port $($node.Port)"
    # Launch through `cmd /c start` instead of Start-Process. Start-Process
    # leaves the spawned process attached to whatever Job Object the
    # invoking PowerShell session sits in — when an automation harness
    # closes that session, the child gets reaped along with it. `cmd start`
    # truly detaches: the child outlives this script regardless of who
    # invoked it.
    $startTitle = "NexusGrid-$($node.Port)"
    cmd /c "start `"$startTitle`" /D `"$($node.Dir)`" `"$exe`" --port $($node.Port)"
}

Write-Host ""
Write-Host "Done ($Choice). node-C on http://127.0.0.1:8000  /  node-D on http://127.0.0.1:8001"
