"""Bounded direct document fetch; call only inside an externally timed worker.

DNS and socket operations can block beyond the monotonic deadline. The caller
must enforce a separate process deadline. Entity bytes exclude HTTP transfer
framing and are accepted only with identity content encoding.
"""

import hashlib
import http.client
import ipaddress
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from source_evidence.document_acquisition_policy import _host
from source_evidence.document_candidates import _safe_url


class _Refusal(Exception):
    def __init__(self, code):
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _public(address):
    if not isinstance(address, str) or "%" in address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    return not (isinstance(ip, ipaddress.IPv6Address) and
                (ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None
                 or ip in ipaddress.ip_network("64:ff9b::/96")
                 or ip in ipaddress.ip_network("64:ff9b:1::/48")))


def _target(url, allowed_hosts):
    try:
        _safe_url(url)
        parsed = urlsplit(url)
        hostname = _host(parsed.hostname)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError, UnicodeError):
        raise _Refusal("unsafe_url") from None
    if hostname not in allowed_hosts:
        raise _Refusal("host_not_allowed")
    if port != (443 if parsed.scheme == "https" else 80):
        raise _Refusal("nondefault_port")
    return parsed, hostname, port


def _valid_answer(item, port):
    if (not isinstance(item, tuple) or len(item) < 5 or item[0] not in (socket.AF_INET, socket.AF_INET6)
            or not isinstance(item[4], tuple) or len(item[4]) < 2 or item[4][1] != port
            or not _public(item[4][0])):
        return False
    ip = ipaddress.ip_address(item[4][0])
    return (ip.version == 4 and item[0] == socket.AF_INET) or (ip.version == 6 and item[0] == socket.AF_INET6)


def fetch(url, *, allowed_hosts, max_bytes, timeout_seconds, max_redirects,
          resolver=socket.getaddrinfo, connection_factory=None):
    """Fetch one URL without retries, ambient credentials, proxies or writes.

    The optional factory receives (scheme, hostname, port, timeout). Resolver
    follows socket.getaddrinfo. All exceptions are reduced to bounded codes.
    """
    if (not isinstance(allowed_hosts, (list, tuple)) or not allowed_hosts
            or any(not isinstance(host, str) for host in allowed_hosts)
            or len(set(allowed_hosts)) != len(allowed_hosts)
            or type(max_bytes) is not int or not 1 <= max_bytes <= 100 * 1024 * 1024
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60
            or type(max_redirects) is not int or not 0 <= max_redirects <= 5):
        raise ValueError("invalid document fetch bounds")
    for host in allowed_hosts:
        _host(host)
    receipt = {"started_at": _now(), "finished_at": None, "request_url": None, "final_url": None,
               "http_status": None, "hops": [], "outcome": "failed", "failure_code": None,
               "body_sha256": None, "body_bytes": None,
               "body_representation": "http_entity_bytes_after_transfer_framing_identity_content_encoding",
               "completion": "incomplete", "deadline_enforcement": "external_worker_required"}
    deadline = time.monotonic() + timeout_seconds
    body = None

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise _Refusal("deadline_exceeded")
        return seconds

    try:
        current = url
        for hop in range(max_redirects + 1):
            parsed, hostname, port = _target(current, allowed_hosts)
            if receipt["request_url"] is None:
                receipt["request_url"] = current
            remaining()
            try:
                addresses = resolver(hostname, port, type=socket.SOCK_STREAM)
            except (OSError, ValueError):
                raise _Refusal("dns_failed") from None
            remaining()
            if not addresses or any(not _valid_answer(item, port) for item in addresses):
                raise _Refusal("destination_not_public")
            chosen = addresses[0][4][0]
            remaining()
            if connection_factory is None:
                conn = (http.client.HTTPSConnection(hostname, port, timeout=remaining(), context=ssl.create_default_context())
                        if parsed.scheme == "https" else http.client.HTTPConnection(hostname, port, timeout=remaining()))
            else:
                conn = connection_factory(parsed.scheme, hostname, port, remaining())

            def pinned(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                if address != (hostname, port):
                    raise _Refusal("connection_target_mismatch")
                return socket.create_connection((chosen, port), min(remaining(), timeout)
                                                if isinstance(timeout, (int, float)) else remaining(), source_address)

            conn._create_connection = pinned
            response = None
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                conn.timeout = remaining()
                conn.request("GET", path, headers={"Accept-Encoding": "identity", "Connection": "close"})
                response_socket = conn.sock
                if response_socket is not None:
                    response_socket.settimeout(remaining())
                response = conn.getresponse()
                status = response.status
                receipt["final_url"] = current
                receipt["http_status"] = status
                receipt["hops"].append({"url": current, "status": status})
                if status in (301, 302, 303, 307, 308):
                    if hop == max_redirects:
                        raise _Refusal("redirect_limit")
                    location = response.getheader("Location")
                    if not location:
                        raise _Refusal("redirect_missing_location")
                    current = urljoin(current, location)
                    continue
                if status != 200:
                    raise _Refusal("http_status")
                encoding = response.getheader("Content-Encoding")
                if encoding is not None and encoding.strip().lower() not in ("", "identity"):
                    raise _Refusal("unsupported_content_encoding")
                length = response.getheader("Content-Length")
                transfer = response.getheader("Transfer-Encoding")
                if transfer is not None:
                    if transfer.strip().lower() != "chunked":
                        raise _Refusal("unsupported_transfer_encoding")
                    if length is not None:
                        raise _Refusal("ambiguous_transfer_framing")
                if length is not None:
                    if not length.isascii() or not length.isdigit():
                        raise _Refusal("invalid_content_length")
                    if int(length) > max_bytes:
                        raise _Refusal("body_too_large")
                chunks = []
                size = 0
                while True:
                    if response_socket is not None:
                        response_socket.settimeout(remaining())
                    remaining()
                    read = getattr(response, "read1", response.read)
                    chunk = read(min(65536, max_bytes - size + 1))
                    remaining()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise _Refusal("body_too_large")
                    chunks.append(chunk)
                if length is not None and size != int(length):
                    raise _Refusal("incomplete_body")
                body = b"".join(chunks)
                receipt.update(outcome="acquired", completion="complete", body_bytes=size,
                               body_sha256=hashlib.sha256(body).hexdigest())
                break
            finally:
                failed_close = False
                for resource in (response, conn):
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception:
                            failed_close = True
                if failed_close and sys.exc_info()[0] is None:
                    raise _Refusal("connection_close_failed")
    except _Refusal as error:
        body = None
        receipt.update(outcome="failed", completion="incomplete", body_bytes=None, body_sha256=None)
        receipt["failure_code"] = error.code
    except (socket.timeout, TimeoutError):
        receipt["failure_code"] = "deadline_or_socket_timeout"
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        receipt["failure_code"] = "transport_error"
    finally:
        receipt["finished_at"] = _now()
    return {"receipt": receipt, "body": body}
