using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Text.RegularExpressions;

// Offline curl substitute: records booleans/counts, never stdin or its URL.
public static class NotificationFixture {
    public static int Main(string[] args) {
        string executable = Process.GetCurrentProcess().MainModule.FileName;
        string mode = Path.GetFileNameWithoutExtension(executable);
        string config = Console.In.ReadToEnd();
        bool secretArg = false, globOff = false, timeout = false, retry = false, redirect = false;
        foreach (string arg in args) {
            secretArg |= arg.Contains("SECRET_CANARY"); globOff |= arg == "--globoff";
            redirect |= arg == "--location" || arg == "-L" || arg == "--insecure" || arg == "-k";
        }
        for (int i = 0; i + 1 < args.Length; i++) {
            timeout |= args[i] == "--max-time" && args[i+1] == "10";
            retry |= args[i] == "--retry" && args[i+1] == "0";
        }
        string attempt = Regex.Match(config, "CRE Windows attempt ([0-9]{8}T[0-9]{6}Z-[0-9a-f]{32})").Groups[1].Value;
        bool start = config.Contains(" started (tier=") || config.Contains(" started."),
            finish = config.Contains(" finished (tier=") || config.Contains(" run finished") || config.Contains(" refresh completed"),
            failed = config.Contains(" failed (tier=") || config.Contains(" needs attention");
        string events = Environment.GetEnvironmentVariable("CRE_TEST_NOTIFICATION_EVENTS");
        if (!String.IsNullOrEmpty(events)) File.AppendAllText(events, start ? "notify-start\n" : "notify-terminal\n");
        string record = "{\"secretArg\":" + secretArg.ToString().ToLowerInvariant() +
            ",\"globOff\":" + globOff.ToString().ToLowerInvariant() +
            ",\"disableFirst\":" + (args.Length > 0 && args[0] == "--disable").ToString().ToLowerInvariant() +
            ",\"timeout10\":" + timeout.ToString().ToLowerInvariant() +
            ",\"retry0\":" + retry.ToString().ToLowerInvariant() +
            ",\"redirectOrInsecure\":" + redirect.ToString().ToLowerInvariant() +
            ",\"hasUrl\":" + config.Contains("url = \"https://example.invalid/SECRET_CANARY").ToString().ToLowerInvariant() +
            ",\"hasData\":" + config.Contains("data = \"{\\\"text\\\":").ToString().ToLowerInvariant() +
            ",\"attemptId\":\"" + attempt + "\",\"started\":" + start.ToString().ToLowerInvariant() +
            ",\"finished\":" + finish.ToString().ToLowerInvariant() + ",\"failed\":" + failed.ToString().ToLowerInvariant() +
            ",\"noDispatchClaim\":" + (config.Contains("Linux dispatch has not been attempted") || config.Contains("collection has not started")).ToString().ToLowerInvariant() +
            ",\"auditFailure\":" + (config.Contains("status=windows_terminal_audit_failed_linux_settlement_unverified") || config.Contains("audit could not be saved")).ToString().ToLowerInvariant() +
            ",\"informationalRun\":" + config.Contains("reference (informational): 2026-10-15T123456Z").ToString().ToLowerInvariant() +
            ",\"readableBrokerage\":" + config.Contains("Hanley Investment Group").ToString().ToLowerInvariant() +
            ",\"technicalSpam\":" + (config.Contains("wrapper_exit=") || config.Contains("native_exit=") || config.Contains("status=")).ToString().ToLowerInvariant() +
            ",\"inputBytes\":" + Encoding.UTF8.GetByteCount(config) + "}\n";
        File.AppendAllText(executable + ".calls.jsonl", record);
        if (mode == "curl-hang") { Thread.Sleep(15000); Console.Write("200"); return 0; }
        if (mode == "curl-28") return 28;
        if (mode == "curl-flood") {
            Thread error = new Thread(delegate() { Flood(Console.OpenStandardError()); });
            error.Start(); Flood(Console.OpenStandardOutput()); error.Join(); return 0;
        }
        if (mode == "curl-noise") Console.Error.Write("https://example.invalid/SECRET_CANARY must be discarded");
        Console.Write(mode == "curl-302" ? "302" : mode == "curl-500" ? "500" : mode == "curl-204" ? "204" : mode == "curl-extra" ? "2000" : "200");
        return 0;
    }
    private static void Flood(Stream stream) {
        byte[] bytes = new byte[8192];
        for (int i = 0; i < bytes.Length; i++) bytes[i] = (byte)'x';
        for (int i = 0; i < 320; i++) stream.Write(bytes, 0, bytes.Length);
    }
}
