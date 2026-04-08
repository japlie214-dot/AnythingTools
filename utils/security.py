# utils/security.py
"""
Security utilities: SSRF mitigation and URL validation helpers.
"""
import socket
import urllib.parse
import ipaddress
from typing import Any


def _is_ip_private(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except Exception:
        return True


def validate_public_url(url: str) -> bool:
    """Validate that `url` is a public http(s) URL and does not resolve to a
    private IP address. Raises ValueError on unsafe URLs.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Unsupported URL scheme")
    if not parsed.hostname:
        raise ValueError("URL missing hostname")

    # Resolve host to IP(s)
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except Exception as e:
        raise ValueError(f"DNS resolution failed: {e}")

    for info in infos:
        addr = info[4][0]
        if _is_ip_private(addr):
            raise ValueError(f"Resolved IP {addr} is private or loopback")
    return True


def scan_args_for_urls(obj: Any) -> None:
    """Recursively scan `obj` (dict/list/str) for URL-like strings and validate them."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            # Common contract: keys containing 'url' are URL fields
            if isinstance(v, str) and ("url" in k.lower() or v.startswith("http://") or v.startswith("https://")):
                validate_public_url(v)
            else:
                scan_args_for_urls(v)
    elif isinstance(obj, list):
        for item in obj:
            scan_args_for_urls(item)
    elif isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            validate_public_url(obj)
    # Other types ignored
