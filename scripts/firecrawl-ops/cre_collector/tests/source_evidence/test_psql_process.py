import subprocess
from types import SimpleNamespace

import pytest
from source_evidence import psql_process


class Stream:
    def __init__(self, fd):
        self.fd = fd
        self.closed = False

    def fileno(self):
        return self.fd

    def close(self):
        self.closed = True


class Selector:
    def __init__(self):
        self.entries = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def register(self, stream, event, data):
        self.entries[stream.fd] = SimpleNamespace(fd=stream.fd, data=data, fileobj=stream)

    def unregister(self, stream):
        del self.entries[stream.fd]

    def get_map(self):
        return self.entries

    def select(self, timeout):
        return [(entry, 0) for entry in list(self.entries.values())]


def fake_io(monkeypatch, *, stdout=b"result", stderr=b"", code=0):
    streams = [Stream(10), Stream(11), Stream(12)]
    process = SimpleNamespace(stdin=streams[0], stdout=streams[1], stderr=streams[2], pid=1234,
                              wait=lambda timeout: code)
    calls = []
    remaining = {11: stdout, 12: stderr}
    sent = bytearray()
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process
    def read(fd, size):
        result, remaining[fd] = remaining[fd][:size], remaining[fd][size:]
        return result
    def write(fd, data):
        assert fd == 10
        sent.extend(bytes(data))
        return len(data)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(psql_process.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(psql_process.os, "set_blocking", lambda *args: None)
    monkeypatch.setattr(psql_process.os, "read", read)
    monkeypatch.setattr(psql_process.os, "write", write)
    monkeypatch.setattr(psql_process, "OwnedProcessGroup", SimpleNamespace(from_spawn=lambda child: child))
    monkeypatch.setattr(psql_process, "settle_owned_groups", lambda *a, **k: SimpleNamespace(settled=True))
    return streams, calls, sent


def test_streams_large_sql_and_both_outputs_without_returning_stderr(monkeypatch):
    streams, calls, sent = fake_io(monkeypatch, stdout=b"result", stderr=b"private notice")
    sql = b"x" * (psql_process.CHUNK_BYTES * 3)
    result = psql_process.run(["psql", "private-uri"], sql, {"PRIVATE": "secret"}, max_results_bytes=20)
    assert result == b"result" and sent == sql
    assert all(stream.closed for stream in streams)
    assert calls[0][1]["start_new_session"] is True and calls[0][1]["shell"] is False


@pytest.mark.parametrize("stdout,stderr,code,reason", [
    (b"too long", b"", 0, "stdout_limit"),
    (b"", b"x" * (64 * 1024 + 1), 0, "stderr_limit"),
    (b"", b"private secret", 1, "nonzero_exit"),
])
def test_output_bounds_and_nonzero_are_coarse_unknown(monkeypatch, stdout, stderr, code, reason):
    streams, _, _ = fake_io(monkeypatch, stdout=stdout, stderr=stderr, code=code)
    with pytest.raises(RuntimeError) as result:
        psql_process.run(["psql", "private-uri"], b"SQL SECRET", {}, max_results_bytes=3)
    assert reason in str(result.value)
    assert all(secret not in str(result.value) for secret in ("private", "SECRET", "uri"))
    assert all(stream.closed for stream in streams)


def test_incomplete_input_and_timeout_are_unknown(monkeypatch):
    fake_io(monkeypatch)
    monkeypatch.setattr(psql_process.os, "write", lambda *args: (_ for _ in ()).throw(BrokenPipeError()))
    with pytest.raises(RuntimeError, match="incomplete_input"):
        psql_process.run(["psql"], b"input", {})
    fake_io(monkeypatch)
    times = iter([0, 46])
    monkeypatch.setattr(psql_process.time, "monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="timeout"):
        psql_process.run(["psql"], b"input", {})


def test_interruption_cannot_hide_unknown_group(monkeypatch):
    streams, _, _ = fake_io(monkeypatch)
    monkeypatch.setattr(Selector, "select", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(psql_process, "settle_owned_groups", lambda *a, **k: SimpleNamespace(settled=False))
    with pytest.raises(psql_process.DocumentProcessCleanupUnknown) as result:
        psql_process.run(["psql"], b"input", {})
    assert isinstance(result.value.__cause__, KeyboardInterrupt)
    assert all(stream.closed for stream in streams)


def test_registration_interruption_is_unknown(monkeypatch):
    fake_io(monkeypatch)
    monkeypatch.setattr(psql_process, "OwnedProcessGroup", SimpleNamespace(
        from_spawn=lambda child: (_ for _ in ()).throw(KeyboardInterrupt())))
    with pytest.raises(psql_process.DocumentProcessCleanupUnknown):
        psql_process.run(["psql"], b"input", {})


def test_close_interrupt_cannot_skip_unknown_settlement(monkeypatch):
    streams, _, _ = fake_io(monkeypatch)
    def close_interrupted():
        raise KeyboardInterrupt()
    streams[1].close = close_interrupted
    calls = []
    def settle(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(settled=False)
    monkeypatch.setattr(psql_process, "settle_owned_groups", settle)
    with pytest.raises(psql_process.DocumentProcessCleanupUnknown) as result:
        psql_process.run(["psql"], b"input", {})
    assert calls and isinstance(result.value.__cause__, KeyboardInterrupt)
    assert streams[0].closed and streams[2].closed


def test_invalid_bounds_refuse_before_spawn(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError):
        psql_process.run(["psql"], b"x" * (psql_process.MAX_SQL_BYTES + 1), {})
    with pytest.raises(ValueError):
        psql_process.run(["psql"], b"", {}, max_results_bytes=0)


def test_unsupported_platform_refuses_before_spawn(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    monkeypatch.setattr(psql_process.os, "killpg", None)
    with pytest.raises(RuntimeError, match="platform unsupported"):
        psql_process.run(["psql"], b"", {})


def test_hex_encoded_sql_can_exceed_32_mib_within_72_mib_cap(monkeypatch):
    assert psql_process.MAX_SQL_BYTES == 72 * 1024 * 1024
    _, _, sent = fake_io(monkeypatch, stdout=b"ok")
    sql = b"x" * (32 * 1024 * 1024 + 1)
    assert psql_process.run(["psql"], sql, {}, max_results_bytes=2) == b"ok"
    assert len(sent) == len(sql)
