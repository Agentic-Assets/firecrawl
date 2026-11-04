// Optional one-shot transport only. Never records the endpoint or child output.
// Compatible with .NET Framework and PowerShell 5.1's C# compiler.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace CreWindowsNotification {
    public sealed class NotificationResult {
        public string Status = "failed";
        public string Reason = "notification_failed";
        public int? HttpStatus;
        public int? TransportExitCode;
        public string NativeSettlement = "not_started";
    }
    public static class Sender {
        private static int poisoned;
        private static Process uncertainProcess; // Retain one uncertain native handle until fresh host exit.
        [StructLayout(LayoutKind.Sequential)]
        private struct FileInformation {
            public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh;
            public uint WriteLow, WriteHigh, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInformation info);
        [DllImport("kernel32.dll")]
        private static extern uint GetFileType(SafeFileHandle handle);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityInfo(SafeFileHandle handle, uint type, uint requested, out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll")]
        private static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentThread();
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr source, IntPtr targetProcess, out IntPtr target, uint access, bool inherit, uint options);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool CancelSynchronousIo(IntPtr thread);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        public static bool IsPrivateDescriptor(byte[] bytes, string currentSid) {
            RawSecurityDescriptor descriptor = new RawSecurityDescriptor(bytes, 0);
            if (descriptor.Owner == null || descriptor.Owner.Value != currentSid || descriptor.DiscretionaryAcl == null) return false;
            foreach (GenericAce ace in descriptor.DiscretionaryAcl) {
                CommonAce rule = ace as CommonAce;
                if (rule == null || rule.IsCallback) return false;
                if (rule.AceQualifier == AceQualifier.AccessDenied) continue;
                if (rule.AceQualifier != AceQualifier.AccessAllowed) return false;
                string sid = rule.SecurityIdentifier.Value;
                if (sid != currentSid && sid != "S-1-5-18" && sid != "S-1-5-32-544") return false;
            }
            return true;
        }
        private static string ReadEndpoint(string path, string excludedRoot) {
            if (!Path.IsPathRooted(path)) throw new IOException();
            string requested = Path.GetFullPath(path);
            if (requested.Length < 4 || requested[1] != ':' || requested[2] != '\\') throw new IOException();
            // READ_DATA, READ_ATTRIBUTES, READ_CONTROL; deny concurrent write/delete.
            using (SafeFileHandle handle = CreateFile(requested, 0x20081, 1, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero)) {
                FileInformation info;
                if (handle.IsInvalid || GetFileType(handle) != 1 || !GetFileInformationByHandle(handle, out info) ||
                    (info.Attributes & (0x10 | 0x400)) != 0 || info.Links != 1 || info.SizeHigh != 0 || info.SizeLow == 0 || info.SizeLow > 4096) throw new IOException();
                StringBuilder final = new StringBuilder(32768);
                uint count = GetFinalPathNameByHandle(handle, final, (uint)final.Capacity, 0);
                string canonical = final.ToString();
                if (count == 0 || count >= final.Capacity || !canonical.StartsWith("\\\\?\\", StringComparison.Ordinal)) throw new IOException();
                canonical = canonical.Substring(4);
                if (canonical.Length < 4 || canonical[1] != ':' || canonical[2] != '\\' || canonical.IndexOf(':', 2) >= 0 ||
                    new DriveInfo(Path.GetPathRoot(canonical)).DriveType != DriveType.Fixed) throw new IOException();
                string excluded = Path.GetFullPath(excludedRoot).TrimEnd('\\') + "\\";
                if (canonical.StartsWith(excluded, StringComparison.OrdinalIgnoreCase)) throw new IOException();
                for (DirectoryInfo directory = Directory.GetParent(canonical); directory != null; directory = directory.Parent) {
                    string git = Path.Combine(directory.FullName, ".git");
                    if (File.Exists(git) || Directory.Exists(git)) throw new IOException();
                }
                IntPtr owner, group, dacl, sacl, descriptor;
                if (GetSecurityInfo(handle, 1, 5, out owner, out group, out dacl, out sacl, out descriptor) != 0) throw new IOException();
                try {
                    uint length = GetSecurityDescriptorLength(descriptor);
                    if (length == 0 || length > 65536) throw new IOException();
                    byte[] security = new byte[length]; Marshal.Copy(descriptor, security, 0, security.Length);
                    using (WindowsIdentity identity = WindowsIdentity.GetCurrent()) {
                        if (!IsPrivateDescriptor(security, identity.User.Value)) throw new IOException();
                    }
                } finally { LocalFree(descriptor); }
                byte[] data = new byte[info.SizeLow];
                using (FileStream input = new FileStream(handle, FileAccess.Read, 4096)) {
                    int offset = 0;
                    while (offset < data.Length) {
                        int size = input.Read(data, offset, data.Length - offset);
                        if (size == 0) throw new IOException(); offset += size;
                    }
                }
                string endpoint = new UTF8Encoding(false, true).GetString(data);
                if (endpoint.EndsWith("\r\n", StringComparison.Ordinal)) endpoint = endpoint.Substring(0, endpoint.Length - 2);
                else if (endpoint.EndsWith("\n", StringComparison.Ordinal)) endpoint = endpoint.Substring(0, endpoint.Length - 1);
                foreach (char character in endpoint) if (Char.IsWhiteSpace(character) || Char.IsControl(character)) throw new IOException();
                Uri uri;
                if (!Uri.TryCreate(endpoint, UriKind.Absolute, out uri) || uri.Scheme != "https" ||
                    uri.UserInfo.Length != 0 || uri.Fragment.Length != 0 || !uri.IsWellFormedOriginalString()) throw new IOException();
                return endpoint;
            }
        }
        public static string EscapeConfig(string value) {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t");
        }
        // Configuration only: reuse the exact handle/ACL/format checks without
        // returning the secret or starting any transport.
        public static bool IsConfigured(string secretFile, string excludedRoot) {
            try { ReadEndpoint(secretFile, excludedRoot); return true; }
            catch { return false; }
        }
        public static NotificationResult Send(string secretFile, string excludedRoot, string executable, string payload) {
            NotificationResult result = new NotificationResult();
            if (String.IsNullOrEmpty(secretFile)) { result.Status = "not_configured"; result.Reason = "not_configured"; return result; }
            if (Interlocked.CompareExchange(ref poisoned, 0, 0) != 0) { result.Status = "unverified"; result.Reason = "fresh_host_required"; result.NativeSettlement = "unknown"; return result; }
            string endpoint;
            try { endpoint = ReadEndpoint(secretFile, excludedRoot); }
            catch { result.Reason = "private_endpoint_rejected"; return result; }
            if (payload == null || Encoding.UTF8.GetByteCount(payload) > 4096) { result.Reason = "payload_rejected"; return result; }
            byte[] config = Encoding.UTF8.GetBytes("url = \"" + EscapeConfig(endpoint) + "\"\ndata = \"" + EscapeConfig(payload) + "\"\n");
            Process process = new Process();
            process.StartInfo.FileName = executable;
            // No endpoint, payload, credential or exception text enters argv/output.
            // --disable must be first: never inherit a user's curlrc options.
            process.StartInfo.Arguments = "--disable --globoff --config - --max-time 10 --connect-timeout 5 --retry 0 --silent --output NUL --write-out \"%{http_code}\" --request POST --header \"Content-Type: application/json\" --proto =https";
            process.StartInfo.UseShellExecute = false; process.StartInfo.CreateNoWindow = true;
            process.StartInfo.RedirectStandardInput = true; process.StartInfo.RedirectStandardOutput = true; process.StartInfo.RedirectStandardError = true;
            result.Status = "attempted"; result.NativeSettlement = "unknown";
            try { if (!process.Start()) throw new IOException(); }
            catch { uncertainProcess = process; Interlocked.Exchange(ref poisoned, 1); result.Status = "unverified"; result.Reason = "start_uncertain"; return result; }
            IoWorker input = null, output = null, error = null;
            byte[] status = new byte[3]; int statusCount = 0;
            try {
                input = new IoWorker(delegate() {
                    Stream stream = process.StandardInput.BaseStream;
                    stream.Write(config, 0, config.Length); stream.Flush(); stream.Close();
                });
                output = new IoWorker(delegate() {
                    byte[] buffer = new byte[1024]; int count;
                    while ((count = process.StandardOutput.BaseStream.Read(buffer, 0, buffer.Length)) != 0) {
                        for (int i = 0; i < count; i++) { if (statusCount < 3) status[statusCount] = buffer[i]; if (statusCount < 4) statusCount++; }
                    }
                    process.StandardOutput.Dispose();
                });
                error = new IoWorker(delegate() {
                    byte[] buffer = new byte[1024];
                    while (process.StandardError.BaseStream.Read(buffer, 0, buffer.Length) != 0) { }
                    process.StandardError.Dispose();
                });
                input.Start(); output.Start(); error.Start();
                bool exited = process.WaitForExit(11000);
                if (exited) { result.TransportExitCode = process.ExitCode; result.NativeSettlement = "exited"; }
                IoWorker[] workers = new IoWorker[] { input, output, error };
                foreach (IoWorker worker in workers) worker.Join(250);
                foreach (IoWorker worker in workers) worker.CancelPending();
                bool joined = true, failed = false;
                foreach (IoWorker worker in workers) { worker.Join(250); joined &= worker.Release(); failed |= worker.Failed; }
                if (!exited || !joined) {
                    uncertainProcess = process;
                    Interlocked.Exchange(ref poisoned, 1); result.Status = "unverified"; result.Reason = "transport_or_io_unsettled";
                    return result;
                }
                process.Dispose();
                int code;
                if (!failed && result.TransportExitCode == 0 && statusCount == 3 && Int32.TryParse(Encoding.ASCII.GetString(status), out code)) {
                    result.HttpStatus = code;
                    if (code >= 200 && code <= 299) { result.Status = "http_accepted"; result.Reason = "http_accepted_not_delivery_proof"; return result; }
                }
                result.Status = "failed"; result.Reason = "transport_or_http_failed";
                return result;
            } catch {
                uncertainProcess = process;
                Interlocked.Exchange(ref poisoned, 1); result.Status = "unverified"; result.Reason = "transport_or_io_uncertain";
                return result;
            }
        }
        private sealed class IoWorker {
            private readonly Thread thread; private IntPtr handle;
            public bool Failed;
            public IoWorker(Action work) {
                thread = new Thread(delegate() {
                    IntPtr retained;
                    if (DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), out retained, 0, false, 2)) Interlocked.Exchange(ref handle, retained);
                    try { work(); } catch { Failed = true; }
                });
                thread.IsBackground = true;
            }
            public void Start() { thread.Start(); }
            public void Join(int milliseconds) { thread.Join(milliseconds); }
            public void CancelPending() {
                if (thread.IsAlive) {
                    Failed = true;
                    IntPtr retained = Interlocked.CompareExchange(ref handle, IntPtr.Zero, IntPtr.Zero);
                    if (retained != IntPtr.Zero) CancelSynchronousIo(retained);
                }
            }
            public bool Release() {
                if (thread.IsAlive) return false;
                if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
                return true;
            }
        }
    }
}
