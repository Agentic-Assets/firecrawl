import hashlib
import socket

import pytest
from source_evidence import document_fetch


def address(ip):
    if ':' in ip:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class Socket:
    def settimeout(self, value):
        self.timeout = value


class Response:
    def __init__(self, status=200, body=b"pdf", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def read1(self, size):
        return self.read(size)

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, scheme, host, port, timeout, response):
        self.scheme, self.host, self.port, self.timeout = scheme, host, port, timeout
        self.response = response
        self.sock = Socket()

    def request(self, method, path, headers):
        self.requested = (method, path, headers)
        self._create_connection((self.host, self.port), self.timeout)

    def getresponse(self):
        if getattr(self.response, "will_close", False):
            self.original_socket = self.sock
            self.sock = None
        return self.response

    def close(self):
        self.closed = True


def run(monkeypatch, responses, *, dns=None, url="https://docs.example.test/file", max_bytes=100,
        redirects=2, hosts=None):
    requests = []
    sockets = []
    monkeypatch.setattr(socket, "create_connection", lambda target, timeout, source_address=None:
                        sockets.append((target, timeout)) or Socket())

    def factory(scheme, host, port, timeout):
        connection = Connection(scheme, host, port, timeout, responses[len(requests)])
        requests.append(connection)
        return connection

    result = document_fetch.fetch(url, allowed_hosts=hosts or ["docs.example.test"], max_bytes=max_bytes,
                                  timeout_seconds=30, max_redirects=redirects,
                                  resolver=dns or (lambda *args, **kwargs: address("8.8.8.8")),
                                  connection_factory=factory)
    return result, requests, sockets


def test_seventy_eight_distinct_responses_pin_original_hostname_and_body(monkeypatch):
    for index in range(78):
        data = f"document-{index}".encode()
        result, connections, sockets = run(monkeypatch, [Response(body=data)],
                                          url=f"https://docs.example.test/file-{index}")
        assert result["body"] == data
        assert result["receipt"]["body_sha256"] == hashlib.sha256(data).hexdigest()
        assert result["receipt"]["completion"] == "complete"
        assert connections[0].host == "docs.example.test"
        assert sockets[0][0] == ("8.8.8.8", 443)
        assert connections[0].requested[2]["Accept-Encoding"] == "identity"
        assert len(connections) == 1


@pytest.mark.parametrize("addresses", [address("127.0.0.1"), address("8.8.8.8") + address("10.0.0.1"),
                                        address("224.0.0.1"), [(socket.AF_INET,)],
                                        address("::ffff:8.8.8.8"), address("64:ff9b::808:808")])
def test_any_private_or_transition_dns_refuses_without_connection(monkeypatch, addresses):
    result, connections, sockets = run(monkeypatch, [], dns=lambda *args, **kwargs: addresses)
    assert result["receipt"]["failure_code"] == "destination_not_public"
    assert not connections and not sockets and result["body"] is None


def test_redirect_revalidates_host_and_dns_without_retry(monkeypatch):
    result, connections, _ = run(monkeypatch, [Response(302, headers={"Location": "https://blocked.example/file"})])
    assert len(connections) == 1
    assert result["receipt"]["failure_code"] == "host_not_allowed"
    assert result["receipt"]["hops"] == [{"url": "https://docs.example.test/file", "status": 302}]
    assert result["receipt"]["final_url"] == "https://docs.example.test/file"


@pytest.mark.parametrize("response,limit,expected", [
    (Response(401), 100, "http_status"), (Response(206), 100, "http_status"),
    (Response(200, headers={"Content-Encoding": "gzip"}), 100, "unsupported_content_encoding"),
    (Response(200, headers={"Transfer-Encoding": "gzip"}), 100, "unsupported_transfer_encoding"),
    (Response(200, headers={"Transfer-Encoding": "chunked", "Content-Length": "3"}), 100, "ambiguous_transfer_framing"),
    (Response(200, body=b"abc", headers={"Content-Length": "4"}), 100, "incomplete_body"),
    (Response(200, body=b"abc"), 2, "body_too_large"),
    (Response(302, headers={"Location": "/again"}), 100, "redirect_limit"),
])
def test_failure_receipts_do_not_claim_complete_body(monkeypatch, response, limit, expected):
    result, connections, _ = run(monkeypatch, [response], max_bytes=limit, redirects=0)
    assert result["receipt"]["failure_code"] == expected
    assert result["receipt"]["body_sha256"] is None and result["body"] is None
    assert result["receipt"]["completion"] == "incomplete" and len(connections) == 1


def test_response_owned_socket_is_timed_and_response_closed(monkeypatch):
    response = Response(body=b"abc")
    response.will_close = True
    result, connections, _ = run(monkeypatch, [response])
    assert result["receipt"]["outcome"] == "acquired"
    assert response.closed and connections[0].closed
    assert connections[0].original_socket.timeout > 0


def test_close_failure_never_claims_completed_body(monkeypatch):
    response = Response(body=b"abc")
    def fail_close():
        raise OSError("private details")
    response.close = fail_close
    result, connections, _ = run(monkeypatch, [response])
    assert result["receipt"]["failure_code"] == "connection_close_failed"
    assert result["receipt"]["completion"] == "incomplete"
    assert result["body"] is None and result["receipt"]["body_sha256"] is None
    assert connections[0].closed


def test_explicit_public_http_reference_uses_port_80_without_url_rewrite(monkeypatch):
    url = "http://docs.example.test/file"
    dns = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
    result, connections, sockets = run(monkeypatch, [Response()], dns=dns, url=url)
    assert result['receipt']['request_url'] == url
    assert result['receipt']['final_url'] == url
    assert result['receipt']['outcome'] == 'acquired'
    assert connections[0].host == 'docs.example.test' and sockets[0][0] == ('8.8.8.8', 80)
