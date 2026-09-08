# Continuously reports the on-screen rect of the video window (a Chrome/Edge
# process launched with --user-data-dir pointing at $ProfileMarker) as
# "left,top,width,height" lines on stdout, one line per change, polled every
# 150ms -- so a parent process (main.js) can keep a separate overlay window
# glued to it without the video window needing to stay at a fixed position
# or size. Runs until killed; never exits on its own.
#
# Reports the line "HIDDEN" instead whenever the video window is minimized,
# not the foreground (focused/on-top) window -- e.g. alt-tabbed away from,
# or simply covered by another window that was clicked to the front -- or
# not found at all (closed). main.js hides the overlay in that case instead
# of leaving it floating over whatever's actually on screen at the
# last-known position.
#
# Chrome spawns several child processes (GPU, renderer, ...) that all
# inherit the same command line, and only one of them owns the actual
# top-level window -- so every matching process is checked rather than
# trusting the first one WMI happens to return (same issue worked around in
# the old launch_overlay.ps1).

param(
    [Parameter(Mandatory = $true)][string]$ProfileMarker
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
public class WinRect {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
"@

# powershell.exe isn't per-monitor-DPI-aware by default, so on a scaled
# monitor GetWindowRect against a DPI-aware window (Chrome/Electron both
# are) returns coordinates Windows has virtualized for us -- garbage like
# (-16000,-16000) with a tiny width/height instead of real screen pixels.
# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4, must be set before any
# window/DPI API calls below.
[WinRect]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null

function Find-VideoWindowHandle {
    $matches = Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($ProfileMarker) }
    foreach ($m in $matches) {
        $p = Get-Process -Id $m.ProcessId -ErrorAction SilentlyContinue
        if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {
            return $p.MainWindowHandle
        }
    }
    return [IntPtr]::Zero
}

$lastLine = $null
while ($true) {
    $hwnd = Find-VideoWindowHandle
    $line = "HIDDEN"
    if ($hwnd -ne [IntPtr]::Zero -and $hwnd -eq [WinRect]::GetForegroundWindow() `
        -and -not [WinRect]::IsIconic($hwnd) -and [WinRect]::IsWindowVisible($hwnd)) {
        $rect = New-Object RECT
        [WinRect]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
        $w = $rect.Right - $rect.Left
        $h = $rect.Bottom - $rect.Top
        if ($w -gt 0 -and $h -gt 0) {
            $line = "$($rect.Left),$($rect.Top),$w,$h"
        }
    }
    if ($line -ne $lastLine) {
        Write-Output $line
        $lastLine = $line
    }
    Start-Sleep -Milliseconds 150
}
