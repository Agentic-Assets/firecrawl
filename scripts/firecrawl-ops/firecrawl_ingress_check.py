#!/usr/bin/env python3
"""Read-only API publication diagnostics; never a firewall or reachability proof."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
from urllib.parse import urlsplit

INSPECT_FORMAT = (
    '{"network_mode":{{json .HostConfig.NetworkMode}},'
    '"ports":{{json .NetworkSettings.Ports}}}'
)


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def endpoint_messages(api_url: str) -> list[str]:
    try:
        endpoint = urlsplit(api_url)
        host = endpoint.hostname
        if endpoint.scheme not in {"http", "https"} or not host:
            raise ValueError
        # Validate the port without printing URL credentials, paths or queries.
        _ = endpoint.port
    except ValueError:
        return [
            "WARNING: API endpoint address is invalid; its ingress posture is unknown."
        ]
    if not is_loopback(host):
        return [
            (
                f"WARNING: API endpoint targets non-loopback host {host!r}; local Docker "
                "inspection does not verify that endpoint's ingress or firewall posture."
            )
        ]
    return []


def publication_messages(records: list[dict]) -> list[str]:
    messages: list[str] = []
    loopback: list[str] = []
    unknown = False
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("network_mode"), str)
            or "ports" not in record
        ):
            unknown = True
            continue
        if record["network_mode"] == "host":
            messages.append(
                "WARNING: API container uses host networking; published ports "
                "cannot establish a loopback-only listener."
            )
            unknown = True
            continue
        if record["network_mode"].startswith(("container:", "service:")):
            messages.append(
                "WARNING: API container shares another container's network namespace; "
                "its own published ports cannot establish ingress exposure."
            )
            unknown = True
            continue
        ports = record.get("ports")
        if ports is None:
            continue
        if not isinstance(ports, dict):
            unknown = True
            continue
        for target, bindings in ports.items():
            if not isinstance(target, str) or not re.fullmatch(
                r"[0-9]{1,5}/(?:tcp|udp|sctp)", target
            ):
                unknown = True
                continue
            if not 1 <= int(target.split("/")[0]) <= 65535:
                unknown = True
                continue
            if bindings is None:
                continue  # Exposed inside Docker, not published on the host.
            if not isinstance(bindings, list):
                unknown = True
                continue
            for binding in bindings:
                if not isinstance(binding, dict):
                    unknown = True
                    continue
                host = binding.get("HostIp")
                port = binding.get("HostPort")
                if (
                    not isinstance(host, str)
                    or not isinstance(port, str)
                    or not re.fullmatch(r"[0-9]{1,5}", port)
                ):
                    unknown = True
                    continue
                if not 1 <= int(port) <= 65535:
                    unknown = True
                    continue
                try:
                    # Docker's empty HostIp is an unspecified/wildcard binding.
                    if host:
                        ipaddress.ip_address(host)
                except ValueError:
                    unknown = True
                    continue
                display = f"[{host}]:{port}" if ":" in host else f"{host or '*'}:{port}"
                if is_loopback(host):
                    loopback.append(f"{display} -> {target}")
                else:
                    messages.append(
                        f"WARNING: Docker API service publishes non-loopback {display} -> {target}; "
                        "a localhost API URL does not establish LAN isolation."
                    )
    if unknown:
        messages.append(
            "WARNING: API publication inspection is incomplete; ingress exposure is unknown."
        )
    elif not messages and loopback:
        messages.append(
            "Ingress: inspected API publications are loopback-only ("
            + "; ".join(loopback)
            + ")."
        )
    elif not messages:
        messages.append(
            "Ingress: no API container ports are published by this Docker service."
        )
    messages.append(
        "Ingress diagnostic does not test host firewall rules, proxies or non-host reachability."
    )
    return messages


def inspect_publications() -> list[str]:
    """Inspect only the selected Compose project's API service, never its env."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--quiet", "api"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        ids = result.stdout.split()
        if (
            not ids
            or len(ids) > 64
            or any(not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in ids)
        ):
            return [
                "WARNING: API container identity unavailable; published ingress exposure is unknown."
            ]
        result = subprocess.run(
            ["docker", "inspect", "--format", INSPECT_FORMAT, *ids],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        records = [
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        ]
        if len(records) != len(ids):
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError):
        return [
            "WARNING: API published-port inspection unavailable or malformed; ingress exposure is unknown."
        ]
    return publication_messages(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:3002")
    args = parser.parse_args()
    for message in endpoint_messages(args.api_url) + inspect_publications():
        print(message)


if __name__ == "__main__":
    main()
