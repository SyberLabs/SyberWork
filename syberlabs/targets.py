"""URL checks for credential-bearing requests. Standard library only."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

from syberlabs.errors import Rejected


_BLOCKED_HOSTS = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.google.com",
})
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".localdomain")


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        return False
    return bool(ip.is_global)


def _https_host_blocked(host: str) -> bool:
    name = host.lower().rstrip(".")
    if name in _BLOCKED_HOSTS or any(name.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return True
    if name.isdigit() or any(label.startswith("0x") for label in name.split(".")):
        return True
    try:
        return not _public_ip(ipaddress.ip_address(name))
    except ValueError:
        return False


def _https_host_listed(host: str) -> bool:
    """``SYBERWORK_HTTPS_HOSTS`` narrows HTTPS. Unset or blank does not."""
    raw = os.getenv("SYBERWORK_HTTPS_HOSTS", "")
    if not raw.strip():
        return True
    allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return host.lower() in allowed


def trusted_origin(url: str) -> tuple[str, str, int]:
    """Reject userinfo, non-loopback HTTP, and non-public HTTPS hosts.

    HTTPS names are checked as written. This function does not resolve DNS.
    """
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (not isinstance(url, str) or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or not parsed.path.startswith("/")):
            raise ValueError("invalid URL")
        if parsed.scheme == "https":
            if _https_host_blocked(parsed.hostname) or not _https_host_listed(parsed.hostname):
                raise ValueError("blocked host")
            return parsed.scheme, parsed.hostname, port or 443
        if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and port:
            return parsed.scheme, parsed.hostname, port
    except (ValueError, AttributeError, TypeError):
        pass
    raise Rejected("invalid_target", "target must be HTTPS or loopback HTTP without URL credentials")


def guard_request(url: str) -> None:
    """Reject an HTTPS target that resolves to a non-public address.

    Loopback HTTP is not resolved. A DNS failure is left to the caller, so an
    unreachable planner stays ``planner_unavailable``.
    """
    scheme, host, port = trusted_origin(url)
    if scheme != "https":
        return
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return
    for info in answers:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise Rejected("invalid_target", "target host resolves to a non-public address") from None
        if not _public_ip(ip):
            raise Rejected("invalid_target", "target host resolves to a non-public address")
