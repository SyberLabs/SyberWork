"""URL checks for credential-bearing requests. Standard library only."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from syberlabs.errors import Rejected


_BLOCKED_HOSTS = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.google.com",
})
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".localdomain")
# A host name the guard classifies must be the name ``urllib.request`` dials. ``urlsplit`` keeps
# percent-escapes in ``hostname`` while ``Request`` unquotes them, so ``%31%32%37.0.0.1`` was checked as a
# name and dialed as ``127.0.0.1``. Only plain ASCII letter-digit-hyphen labels (IDNA names in their
# ``xn--`` form) and IP literals are accepted; anything else is refused before it is classified.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}\.?", re.ASCII)


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


def _plain_host(host: str) -> bool:
    """A host ``urllib.request.Request`` dials exactly as written: an IP literal or plain LDH labels."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return len(host) <= 253 and bool(_HOSTNAME.fullmatch(host))


def _dialed_host(url: str) -> str | None:
    """The host ``urllib.request`` would connect to for ``url``, without its port and brackets."""
    try:
        dialed = urllib.request.Request(url).host
    except ValueError:
        return None
    found = urlsplit("//" + dialed).hostname
    return found.lower() if found else None


def _https_host_listed(host: str) -> bool:
    """``SYBERWORK_HTTPS_HOSTS`` narrows HTTPS. Unset or blank does not."""
    raw = os.getenv("SYBERWORK_HTTPS_HOSTS", "")
    if not raw.strip():
        return True
    allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return host.lower() in allowed


def trusted_origin(url: str) -> tuple[str, str, int]:
    """Reject userinfo, non-loopback HTTP, and non-public HTTPS hosts.

    HTTPS names are checked as written, and the name checked must be the name ``urllib.request`` dials:
    a host with percent-escapes, other non-LDH characters or non-ASCII is refused. This function does not
    resolve DNS.
    """
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (not isinstance(url, str) or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or not parsed.path.startswith("/")):
            raise ValueError("invalid URL")
        if not _plain_host(parsed.hostname) or _dialed_host(url) != parsed.hostname:
            raise ValueError("host is not dialed as written")
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

    Loopback HTTP is not resolved. A name the guard cannot resolve is refused, not allowed: it raises
    ``urllib.error.URLError`` (no request is made), so each caller keeps the code it already gives an
    unreachable destination (``planner_unavailable``, ``source_unavailable``, ``destination_unreachable``).
    """
    scheme, host, port = trusted_origin(url)
    if scheme != "https":
        return
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise urllib.error.URLError("target host did not resolve") from None
    if not answers:
        raise urllib.error.URLError("target host did not resolve")
    for info in answers:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise Rejected("invalid_target", "target host resolves to a non-public address") from None
        if not _public_ip(ip):
            raise Rejected("invalid_target", "target host resolves to a non-public address")


def credentialed_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """An opener for credential-bearing requests that never routes through an environment proxy.

    ``urllib.request.build_opener`` adds a ``ProxyHandler`` that reads ``http_proxy``/``https_proxy`` when the
    opener is built. Through a proxy the guard's address check is moot (the proxy resolves and dials) and the
    bearer token goes to the proxy, including on loopback HTTP, which the guard does not resolve. The empty
    ``ProxyHandler`` disables that; a deployment that needs egress through a proxy must use a transparent one.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)
