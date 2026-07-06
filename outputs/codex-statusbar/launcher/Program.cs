using System.Diagnostics;

var exeName = Path.GetFileNameWithoutExtension(Environment.ProcessPath ?? string.Empty).ToLowerInvariant();
var baseDir = AppContext.BaseDirectory;

var scriptName = exeName switch
{
    "codex-watchdog" => "run-watchdog.ps1",
    "codex-statusbar" => "run-statusbar.ps1",
    "codex-stat" => "run-statusbar.ps1",
    _ => ""
};

if (scriptName.Length == 0)
{
    Console.Error.WriteLine($"Unknown launcher name: {exeName}");
    return 2;
}

var scriptPath = Path.Combine(baseDir, scriptName);
if (!File.Exists(scriptPath))
{
    Console.Error.WriteLine($"Cannot find {scriptPath}");
    return 2;
}

var powershell = FindOnPath("pwsh.exe") ?? FindOnPath("powershell.exe") ?? "powershell.exe";
var startInfo = new ProcessStartInfo
{
    FileName = powershell,
    UseShellExecute = false,
};

startInfo.ArgumentList.Add("-NoProfile");
startInfo.ArgumentList.Add("-ExecutionPolicy");
startInfo.ArgumentList.Add("Bypass");
startInfo.ArgumentList.Add("-File");
startInfo.ArgumentList.Add(scriptPath);
foreach (var arg in args)
{
    startInfo.ArgumentList.Add(arg);
}

using var process = Process.Start(startInfo);
if (process is null)
{
    Console.Error.WriteLine($"Failed to start {powershell}");
    return 1;
}

process.WaitForExit();
return process.ExitCode;

static string? FindOnPath(string fileName)
{
    var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
    foreach (var dir in path.Split(Path.PathSeparator))
    {
        if (string.IsNullOrWhiteSpace(dir))
        {
            continue;
        }
        var candidate = Path.Combine(dir.Trim(), fileName);
        if (File.Exists(candidate))
        {
            return candidate;
        }
    }
    return null;
}
