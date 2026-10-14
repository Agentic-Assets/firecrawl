using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

// Synthetic native executable. No network, WSL, signals or production paths.
public static class CaptureFixture {
    public static int Main(string[] args) {
        string mode = args.Length == 0 ? "default" : args[0];
        if (mode == "holder") { Thread.Sleep(4000); return 0; }
        if (mode == "marker") { File.WriteAllText(args[1], "dispatched"); return 0; }
        string name = Path.GetFileNameWithoutExtension(Process.GetCurrentProcess().MainModule.FileName);
        if (name == "nonzero" || name == "held-pipes" || name == "flood") mode = name;
        if (mode == "held-pipes") {
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = Process.GetCurrentProcess().MainModule.FileName;
            info.Arguments = "holder"; info.UseShellExecute = false; info.CreateNoWindow = true;
            // Inherit the redirected standard handles, outliving this parent.
            Process.Start(info); return 0;
        }
        if (mode == "flood") {
            Thread error = new Thread(delegate() { Flood(Console.OpenStandardError(), (byte)'E'); });
            error.Start(); Flood(Console.OpenStandardOutput(), (byte)'O'); error.Join(); return 0;
        }
        if (mode == "argv") {
            for (int i = 1; i < args.Length; i++)
                Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(args[i])));
            return 0;
        }
        if (mode == "utf8") {
            byte[] bytes = Encoding.UTF8.GetBytes("snowman \u2603 astral \ud83d\ude80 caf\u00e9\n");
            Console.OpenStandardOutput().Write(bytes, 0, bytes.Length); return 0;
        }
        if (mode == "refs") {
            Console.WriteLine(args[1] + "2026-10-13T183401Z-0123456789ab");
            Console.WriteLine(args[1] + "2026-10-13T183402Z-0123456789ab"); return 0;
        }
        Console.WriteLine("synthetic stdout"); Console.Error.WriteLine("synthetic stderr");
        return mode == "nonzero" ? 23 : 0;
    }
    private static void Flood(Stream stream, byte value) {
        byte[] block = new byte[8192]; for (int i = 0; i < block.Length; i++) block[i] = value;
        for (int i = 0; i < 320; i++) stream.Write(block, 0, block.Length);
        stream.Flush();
    }
}
