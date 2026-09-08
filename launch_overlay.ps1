# Opens the primary jellyfish-tracking overlay page (http://localhost:8000/)
# as a standalone, chrome-less "app mode" popup window -- no tabs, no address
# bar, no bookmarks bar -- so it can sit on screen (or be captured by OBS as
# a Window Capture source instead of a Browser Source) like a native app.
#
# Requires main.py to already be running (the dashboard must be up at
# DASHBOARD_HOST:DASHBOARD_PORT from config.py -- defaults below match
# config.py's defaults; update both places together if you change config.py).
#
# Usage: right-click -> Run with PowerShell, or from a terminal:
#   powershell -ExecutionPolicy Bypass -File launch_overlay.ps1
#
# Re-running this script does NOT open a second window -- if the overlay
# popup is already open (including minimized), it just restores, resizes,
# and focuses the existing one.

$url = "http://localhost:8000/"
$width = 1280
$height = 720
$left = 0
$top = 0

# A dedicated browser profile, separate from your normal Chrome/Edge profile.
# This is the key fix for buggy sizing: if you already have Chrome open,
# `--app=...` on the default profile gets forwarded via IPC to that existing
# process, and Chrome silently ignores --window-size/--window-position for
# an IPC-forwarded request -- only a genuinely new browser process honors
# them. A dedicated --user-data-dir guarantees this always launches (or is
# found as) its own independent process.
$profileDir = Join-Path $env:LOCALAPPDATA "JellyfishOverlayProfile"

$candidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$browser = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $browser) {
    Write-Host "Could not find Chrome or Edge in the usual install locations."
    Write-Host "Open this URL manually in any browser instead: $url"
    exit 1
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class OverlayWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
}
"@
$SW_RESTORE = 9

function Set-OverlayWindowState($hwnd) {
    # Explicitly restore (un-minimize), force the exact size/position, and
    # focus -- done directly via Win32 calls rather than relying on the
    # browser's own interpretation of command-line flags, which is what was
    # buggy before.
    [OverlayWindow]::ShowWindow($hwnd, $SW_RESTORE) | Out-Null
    [OverlayWindow]::MoveWindow($hwnd, $left, $top, $width, $height, $true) | Out-Null
    [OverlayWindow]::SetForegroundWindow($hwnd) | Out-Null
}

# Look for an already-running overlay window (a browser process using our
# dedicated profile dir) before launching a new one. Chrome/Edge spawn many
# child processes (GPU, renderer, ...) that all inherit the same command
# line, and only one of them owns the actual top-level window -- so check
# every match rather than trusting the first one WMI happens to return.
$existingMatches = Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profileDir) }

$existingHwnd = [IntPtr]::Zero
foreach ($m in $existingMatches) {
    $p = Get-Process -Id $m.ProcessId -ErrorAction SilentlyContinue
    if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {
        $existingHwnd = $p.MainWindowHandle
        break
    }
}

if ($existingHwnd -ne [IntPtr]::Zero) {
    Write-Host "Overlay popup already open -- restoring and resizing it."
    Set-OverlayWindowState $existingHwnd
    exit 0
}

Write-Host "Launching overlay popup via $browser ..."
$proc = Start-Process -FilePath $browser -PassThru -ArgumentList @(
    "--app=$url",
    "--user-data-dir=$profileDir",
    "--window-size=$width,$height",
    "--window-position=$left,$top",
    "--no-first-run",
    "--no-default-browser-check"
)

$deadline = (Get-Date).AddSeconds(10)
$hwnd = [IntPtr]::Zero
while ((Get-Date) -lt $deadline -and $hwnd -eq [IntPtr]::Zero) {
    Start-Sleep -Milliseconds 200
    $proc.Refresh()
    if ($proc.MainWindowHandle -ne [IntPtr]::Zero) {
        $hwnd = $proc.MainWindowHandle
    }
}

if ($hwnd -eq [IntPtr]::Zero) {
    Write-Host "Popup launched, but its window couldn't be located to size/position it automatically -- resize it manually if needed."
} else {
    Set-OverlayWindowState $hwnd
    Write-Host "Overlay popup opened at ${width}x${height}."
}
