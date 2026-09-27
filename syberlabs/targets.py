"""URL checks for credential-bearing requests. Standard library only."""

from __future__ import annotations

from urllib.parse import urlsplit

from syberlabs.errors import Rejected


def trusted_origin(url: str) -> tuple[str, str, int]:
    """Reject userinfo and non-loopback HTTP before a credential-bearing request."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (not isinstance(url, str) or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or not parsed.path.startswith("/")):
            raise ValueError("invalid URL")
        if parsed.scheme == "https":
            return parsed.scheme, parsed.hostname, port or 443
        if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and port:
            return parsed.scheme, parsed.hostname, port
    except (ValueError, AttributeError, TypeError):
        pass
    raise Rejected("invalid_target", "target must be HTTPS or loopback HTTP without URL credentials")
