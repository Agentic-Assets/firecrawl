// Fork-owned Windows foreground capture. Compatible with .NET Framework/C# 5.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace CreWindowsCapture {
    public sealed class StreamResult {
        public long ObservedBytes;
        public long CapturedBytes; // Successfully completed writes; partial failed IO is unknown.
        public bool Truncated;
        public bool Eof;
        public bool ReadFailed;
        public bool WriteFailed;
        public bool FlushFailed;
        public bool CloseFailed;
        public bool DrainTimedOut;
        public bool ReaderDidNotStop;
        public string ObservedRunDirectory;
        public bool RunReferenceConflict;
        public bool Failed { get { return ReadFailed || WriteFailed || FlushFailed || CloseFailed || DrainTimedOut || ReaderDidNotStop; } }
    }
    public sealed class CaptureResult {
        public bool StartAttempted;
        public bool Started;
        public bool StartFailed;
        public bool ReadOnlyDeadlineExpired;
        public int? NativeExitCode;
        public StreamResult Stdout = new StreamResult();
        public StreamResult Stderr = new StreamResult();
        public bool Failed { get { return StartFailed || ReadOnlyDeadlineExpired || Stdout.Failed || Stderr.Failed; } }
    }
    public static class Capture {
        public const int ByteLimit = 1048576;
        private static int poisoned;
        private static void RequireReusable() {
            if (Interlocked.CompareExchange(ref poisoned, 0, 0) != 0)
                throw new InvalidOperationException("Capture host is unusable; start a fresh -File shell.");
        }
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern IntPtr GetCurrentThread();
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr source, IntPtr targetProcess, out IntPtr target, uint access, bool inherit, uint options);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool CancelSynchronousIo(IntPtr thread);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        // Windows CRT quoting, including empty args and backslashes before quotes/end.
        public static string Quote(string value) {
            if (value == null || value.IndexOf('\0') >= 0) throw new ArgumentException("Invalid native argument.");
            // Native option parsers such as WSL require ordinary atoms unquoted.
            bool needsQuotes = value.Length == 0;
            foreach (char c in value) {
                if (Char.IsWhiteSpace(c) || c == '"') { needsQuotes = true; break; }
            }
            if (!needsQuotes) return value;
            StringBuilder b = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value) {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append('"'); }
                else { b.Append('\\', slashes); b.Append(c); }
                slashes = 0;
            }
            b.Append('\\', slashes * 2); b.Append('"');
            return b.ToString();
        }
        public static CaptureResult Run(string executable, string[] arguments, string stdoutPath, string stderrPath, string runPrefix) {
            RequireReusable();
            foreach (string argument in arguments) Quote(argument);
            // Both unique sinks must exist before any dispatch attempt.
            Stream output = new FileStream(stdoutPath, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            Stream error;
            try { error = new FileStream(stderrPath, FileMode.CreateNew, FileAccess.Write, FileShare.Read); }
            catch { output.Dispose(); throw; }
            return RunWithStreams(executable, arguments, output, error, runPrefix, 2000);
        }
        // Only for an independently spawned read-only summary frontend. Never
        // use this path for the foreground pipeline or infer Linux settlement.
        public static CaptureResult RunReadOnly(string executable, string[] arguments, string stdoutPath, string stderrPath, int deadlineMilliseconds) {
            RequireReusable();
            if (deadlineMilliseconds < 50 || deadlineMilliseconds > 15000) throw new ArgumentOutOfRangeException("deadlineMilliseconds");
            foreach (string argument in arguments) Quote(argument);
            Stream output = new FileStream(stdoutPath, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            Stream error;
            try { error = new FileStream(stderrPath, FileMode.CreateNew, FileAccess.Write, FileShare.Read); }
            catch { output.Dispose(); throw; }
            return RunCore(executable, arguments, output, error, null, 2000, deadlineMilliseconds);
        }
        // Stream/deadline seam exercises the shipped drain with synthetic IO faults.
        // Ownership of both sinks transfers here; no PowerShell callbacks run on threads.
        public static CaptureResult RunWithStreams(string executable, string[] arguments, Stream output, Stream error, string runPrefix, int drainMilliseconds) {
            return RunCore(executable, arguments, output, error, runPrefix, drainMilliseconds, 0);
        }
        private static CaptureResult RunCore(string executable, string[] arguments, Stream output, Stream error, string runPrefix, int drainMilliseconds, int readOnlyDeadlineMilliseconds) {
            RequireReusable();
            if (drainMilliseconds < 1 || drainMilliseconds > 5000) throw new ArgumentOutOfRangeException("drainMilliseconds");
            CaptureResult result = new CaptureResult();
            Process process = new Process();
            process.StartInfo.FileName = executable;
            StringBuilder command = new StringBuilder();
            foreach (string arg in arguments) { if (command.Length > 0) command.Append(' '); command.Append(Quote(arg)); }
            process.StartInfo.Arguments = command.ToString();
            process.StartInfo.UseShellExecute = false;
            process.StartInfo.CreateNoWindow = true;
            process.StartInfo.RedirectStandardOutput = true;
            process.StartInfo.RedirectStandardError = true;
            result.StartAttempted = true;
            try { result.Started = process.Start(); }
            catch { result.StartFailed = true; Interlocked.Exchange(ref poisoned, 1); }
            if (!result.Started) {
                result.StartFailed = true;
                FinishSink(output, result.Stdout); FinishSink(error, result.Stderr);
                process.Dispose(); return result;
            }
            Reader stdout = null, stderr = null;
            try {
                stdout = new Reader(process.StandardOutput.BaseStream, output, result.Stdout, runPrefix);
                stderr = new Reader(process.StandardError.BaseStream, error, result.Stderr, null);
                stdout.Start(); stderr.Start();
                if (readOnlyDeadlineMilliseconds == 0) {
                    // Foreground wait only. Never terminate the pipeline process or its children.
                    process.WaitForExit();
                    result.NativeExitCode = process.ExitCode;
                } else if (process.WaitForExit(readOnlyDeadlineMilliseconds)) {
                    result.NativeExitCode = process.ExitCode;
                } else {
                    result.ReadOnlyDeadlineExpired = true;
                    // This exact Process is the separate read-only frontend,
                    // never the pipeline frontend. No Linux settlement claim.
                    try { process.Kill(); } catch { }
                    if (process.WaitForExit(1000)) {
                        result.NativeExitCode = process.ExitCode;
                    } else {
                        Interlocked.Exchange(ref poisoned, 1);
                    }
                }
                Stopwatch deadline = Stopwatch.StartNew();
                stdout.Join(drainMilliseconds);
                stderr.Join(Math.Max(0, drainMilliseconds - (int)deadline.ElapsedMilliseconds));
                stdout.CancelIfPending(); stderr.CancelIfPending();
                stdout.Join(1000); stderr.Join(1000);
                stdout.ReleaseHandle(); stderr.ReleaseHandle();
                if (stdout.IsAlive || stderr.IsAlive) Interlocked.Exchange(ref poisoned, 1);
                // Cross-thread Dispose can itself block on a pending read. A failed join
                // deliberately leaves those background resources to this one-shot host's
                // exit, with explicit failure evidence, rather than blocking or killing.
                // Operators must use a fresh -File shell, not a reusable host/runspace.
                if (!stdout.IsAlive && !stderr.IsAlive) process.Dispose();
                return result;
            } catch {
                // Setup/wait uncertainty after Start cannot safely support reuse.
                // No attempt to infer or force native/Linux settlement.
                Interlocked.Exchange(ref poisoned, 1);
                throw;
            }
        }
        private static void FinishSink(Stream sink, StreamResult result) {
            try { FileStream file = sink as FileStream; if (file != null) file.Flush(true); else sink.Flush(); }
            catch { result.FlushFailed = true; }
            try { sink.Dispose(); } catch { result.CloseFailed = true; }
        }
        private sealed class Reader {
            private readonly Stream input, sink;
            private readonly StreamResult result;
            private readonly string prefix;
            private readonly Thread thread;
            private volatile bool stop;
            private IntPtr handle;
            public bool IsAlive { get { return thread.IsAlive; } }
            public Reader(Stream input, Stream sink, StreamResult result, string prefix) {
                this.input = input; this.sink = sink; this.result = result; this.prefix = prefix;
                thread = new Thread(Drain); thread.IsBackground = true;
            }
            public void Start() { thread.Start(); }
            public void Join(int milliseconds) { thread.Join(milliseconds); }
            public void CancelIfPending() {
                if (!thread.IsAlive) return;
                result.DrainTimedOut = true; stop = true;
                IntPtr current = Interlocked.CompareExchange(ref handle, IntPtr.Zero, IntPtr.Zero);
                if (current != IntPtr.Zero) CancelSynchronousIo(current);
            }
            public void ReleaseHandle() {
                if (thread.IsAlive) { result.ReaderDidNotStop = true; return; }
                if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
            }
            private void Drain() {
                IntPtr retained;
                if (DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), out retained, 0, false, 2))
                    Interlocked.Exchange(ref handle, retained); // retain actual owned thread
                byte[] bytes = new byte[8192];
                char[] chars = new char[Encoding.UTF8.GetMaxCharCount(8192)];
                Decoder decoder = new UTF8Encoding(false, false).GetDecoder();
                StringBuilder line = new StringBuilder();
                bool lineTooLong = false;
                try {
                    while (!stop) {
                        int count;
                        try { count = input.Read(bytes, 0, bytes.Length); }
                        catch { if (!stop) result.ReadFailed = true; break; }
                        if (count == 0) { result.Eof = true; break; }
                        if (Interlocked.Add(ref result.ObservedBytes, count) > ByteLimit) result.Truncated = true;
                        int keep = (int)Math.Min(count, ByteLimit - Interlocked.Read(ref result.CapturedBytes));
                        if (keep < count) result.Truncated = true;
                        if (keep > 0 && !result.WriteFailed) {
                            try { sink.Write(bytes, 0, keep); Interlocked.Add(ref result.CapturedBytes, keep); }
                            catch { result.WriteFailed = true; }
                        }
                        if (prefix != null) {
                            int length = decoder.GetChars(bytes, 0, count, chars, 0, false);
                            for (int i = 0; i < length; i++) {
                                char c = chars[i];
                                if (c == '\n') {
                                    if (!lineTooLong) ObserveLine(line.ToString().TrimEnd('\r'));
                                    line.Length = 0; lineTooLong = false;
                                } else if (line.Length < 4096) line.Append(c);
                                else lineTooLong = true;
                            }
                        }
                    }
                } catch { result.ReadFailed = true; }
                finally {
                    FinishSink(sink, result);
                    try { input.Dispose(); } catch { result.CloseFailed = true; }
                }
            }
            private void ObserveLine(string line) {
                if (!line.StartsWith(prefix, StringComparison.Ordinal)) return;
                string run = line.Substring(prefix.Length);
                if (!Regex.IsMatch(run, "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z(?:-[0-9a-f]{12})?$")) return;
                if (result.ObservedRunDirectory == null) result.ObservedRunDirectory = line;
                else if (result.ObservedRunDirectory != line) result.RunReferenceConflict = true;
            }
        }
    }
}
