param(
  [Parameter(Mandatory = $true)]
  [string]$OutputPath,
  [int]$DurationMs = 60000
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

public static class CodexHubMouseHook
{
    public delegate IntPtr LowLevelMouseProc(int nCode, IntPtr wParam, IntPtr lParam);

    public struct MouseEvent
    {
        public long callbackEpochMs;
        public long epochMs;
        public int flags;
        public int mouseData;
        public int time;
        public string type;
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT
    {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MSLLHOOKSTRUCT
    {
        public POINT pt;
        public int mouseData;
        public int flags;
        public int time;
        public IntPtr dwExtraInfo;
    }

    public const int WH_MOUSE_LL = 14;
    public const int WM_LBUTTONDOWN = 0x0201;
    public const int WM_LBUTTONUP = 0x0202;

    public static readonly List<MouseEvent> Events = new List<MouseEvent>();
    private static readonly long StartEpochMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    private static readonly long StartTickMs = (long)GetTickCount64();
    private static LowLevelMouseProc proc = HookCallback;
    private static IntPtr hookId = IntPtr.Zero;

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelMouseProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr GetModuleHandle(string lpModuleName);

    [DllImport("kernel32.dll")]
    private static extern ulong GetTickCount64();

    public static void Start()
    {
        using (Process currentProcess = Process.GetCurrentProcess())
        using (ProcessModule currentModule = currentProcess.MainModule)
        {
            hookId = SetWindowsHookEx(WH_MOUSE_LL, proc, GetModuleHandle(currentModule.ModuleName), 0);
            if (hookId == IntPtr.Zero)
            {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
        }
    }

    public static void Stop()
    {
        if (hookId != IntPtr.Zero)
        {
            UnhookWindowsHookEx(hookId);
            hookId = IntPtr.Zero;
        }
    }

    private static IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0)
        {
            int message = wParam.ToInt32();
            if (message == WM_LBUTTONDOWN || message == WM_LBUTTONUP)
            {
                MSLLHOOKSTRUCT data = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(MSLLHOOKSTRUCT));
                long callbackEpochMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                long eventEpochMs = StartEpochMs + ((long)data.time - StartTickMs);
                Events.Add(new MouseEvent
                {
                    callbackEpochMs = callbackEpochMs,
                    epochMs = eventEpochMs,
                    flags = data.flags,
                    mouseData = data.mouseData,
                    time = data.time,
                    type = message == WM_LBUTTONDOWN ? "down" : "up",
                    x = data.pt.x,
                    y = data.pt.y
                });
            }
        }
        return CallNextHookEx(hookId, nCode, wParam, lParam);
    }
}
"@

$directory = Split-Path -Parent $OutputPath
if ($directory) {
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

[CodexHubMouseHook]::Start()
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(100, $DurationMs)
$timer.Add_Tick({
  $timer.Stop()
  [System.Windows.Forms.Application]::ExitThread()
})
$timer.Start()
[System.Windows.Forms.Application]::Run()
[CodexHubMouseHook]::Stop()

$events = [CodexHubMouseHook]::Events | ForEach-Object {
  [pscustomobject]@{
    callbackEpochMs = $_.callbackEpochMs
    epochMs = $_.epochMs
    flags = $_.flags
    mouseData = $_.mouseData
    time = $_.time
    type = $_.type
    x = $_.x
    y = $_.y
  }
}
$json = if (@($events).Count -eq 0) {
  "[]"
} else {
  @($events) | ConvertTo-Json -Depth 4
}
[System.IO.File]::WriteAllText(
  $OutputPath,
  $json,
  (New-Object System.Text.UTF8Encoding($false))
)
