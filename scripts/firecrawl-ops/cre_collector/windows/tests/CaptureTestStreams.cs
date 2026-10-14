using System;
using System.IO;
using System.Text;
using System.Threading;

namespace CreCaptureTests {
    public sealed class BlockingStream : MemoryStream {
        public readonly ManualResetEvent Release = new ManualResetEvent(false);
        public override void Write(byte[] data, int offset, int count) {
            Release.WaitOne(); base.Write(data, offset, count);
        }
    }
    public sealed class FaultStream : MemoryStream {
        private readonly string fault;
        public FaultStream(string fault) { this.fault = fault; }
        public override void Write(byte[] data, int offset, int count) {
            if (fault == "write") throw new IOException("synthetic");
            base.Write(data, offset, count);
        }
        public override void Flush() { if (fault == "flush") throw new IOException("synthetic"); base.Flush(); }
        protected override void Dispose(bool disposing) {
            base.Dispose(disposing); if (fault == "close") throw new IOException("synthetic");
        }
    }
    public sealed class ChunkStream : MemoryStream {
        private int call;
        public override int Read(byte[] buffer, int offset, int count) {
            if (call++ == 0) { buffer[offset] = 0xf0; buffer[offset+1] = 0x9f; buffer[offset+2] = 0x9a; return 3; }
            if (call == 2) { buffer[offset] = 0x80; for (int i = 1; i < 8192; i++) buffer[offset+i] = (byte)'x'; return 8192; }
            return 0;
        }
    }
}
