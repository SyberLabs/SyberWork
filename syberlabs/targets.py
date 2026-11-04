"""URL checks for credential-bearing requests. Standard library only."""

from __future__ import annotations

import http.client
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


# IANA special-purpose ranges (RFC 6890 registries) that are not a destination for a credentialed request.
# ``ipaddress`` flags most of them, but not all, and not the same set on every interpreter: some versions call
# 192.0.0.9, 192.0.0.10, 2001:1::1, 2001:1::2 and the deprecated site-local fec0::/10 global. They are listed here
# so the answer does not depend on the Python release. Anycast service addresses (PCP, TURN) are refused too:
# no installed source, action or planner is one of them.
_SPECIAL_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.31.196.0/24", "192.52.193.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "192.175.48.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/127", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16",
    "2620:4f:8000::/48", "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
))
# NAT64 (64:ff9b::/96) carries an IPv4 address in its last 32 bits; the gateway dials that address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.version == 6 and ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if any(ip.version == net.version and ip in net for net in _SPECIAL_NETWORKS):
        return False
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


def _accepted_addresses(host: str, port: int) -> list[tuple]:
    """Resolve ``host`` once and return its addresses, refusing the lot if any of them is not public.

    A name that does not resolve raises ``urllib.error.URLError`` (callers report their unreachable code); a
    name with any non-public address raises ``Rejected("invalid_target")``.
    """
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
    return answers


def guard_request(url: str) -> None:
    """Reject an HTTPS target that resolves to a non-public address.

    Loopback HTTP is not resolved. A name the guard cannot resolve is refused, not allowed: it raises
    ``urllib.error.URLError`` (no request is made), so each caller keeps the code it already gives an
    unreachable destination (``planner_unavailable``, ``source_unavailable``, ``destination_unreachable``).
    This early check gives callers their codes; the connection itself is pinned by ``credentialed_opener``,
    which resolves and checks again and dials only the addresses it accepted.
    """
    scheme, host, port = trusted_origin(url)
    if scheme != "https":
        return
    _accepted_addresses(host, port)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that dials only addresses the guard accepts.

    ``urllib`` would otherwise resolve the name a second time when it connects, so a name whose answer changes
    between the guard's lookup and the connection (DNS rebinding) could reach a private address. Here the
    lookup and the check happen in ``connect`` and the socket goes to one of the addresses just accepted. The
    original name is still used for SNI, certificate verification and the Host header.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = self._pinned_connection

    def _pinned_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        host, port = address
        try:
            answers = _accepted_addresses(host, port)
        except Rejected as refused:
            raise OSError(f"refused target address: {refused.detail}") from None
        except urllib.error.URLError as unresolved:
            raise OSError(str(unresolved.reason)) from None
        last = None
        for family, kind, proto, _name, sockaddr in answers:
            sock = None
            try:
                sock = socket.socket(family, kind, proto)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                last = error
                if sock is not None:
                    sock.close()
        raise last if last is not None else OSError("no address to connect to")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


def credentialed_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """An opener for credential-bearing requests that never routes through an environment proxy.

    ``urllib.request.build_opener`` adds a ``ProxyHandler`` that reads ``http_proxy``/``https_proxy`` when the
    opener is built. Through a proxy the guard's address check is moot (the proxy resolves and dials) and the
    bearer token goes to the proxy, including on loopback HTTP, which the guard does not resolve. The empty
    ``ProxyHandler`` disables that; a deployment that needs egress through a proxy must use a transparent one.
    HTTPS connections are pinned: each resolves and checks its host and dials only the accepted addresses.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTPSHandler(), *handlers)


def header_value_ok(value: str) -> bool:
    """A value safe to place in an HTTP header: printable, no CR or LF, no leading or trailing whitespace.

    ``http.client`` refuses a bare line break but accepts an obsolete fold (a break followed by a space or
    tab), which a later hop may read as a second header. Anything outside visible ASCII and inner spaces
    is refused rather than encoded.
    """
    return (isinstance(value, str) and value == value.strip(" \t") and value != ""
            and all(" " <= ch <= "~" for ch in value))
