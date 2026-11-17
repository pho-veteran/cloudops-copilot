"""Canonical server/CA identity shared by independently deployed read-only consumers."""

import base64
import hashlib
import re
import ssl
from urllib.parse import urlsplit


def server_identity(server: str) -> str:
    if not isinstance(server, str) or any(ord(c) <= 32 or ord(c) == 127 for c in server):
        raise ValueError
    parsed = urlsplit(server)
    host, port = parsed.hostname, parsed.port
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or "?" in server
        or "#" in server
        or "\\" in server
        or "%" in host
        or parsed.netloc.endswith(":")
    ):
        raise ValueError
    host = host.encode("idna").decode("ascii").lower()
    if ":" in host:
        import ipaddress

        host = f"[{ipaddress.IPv6Address(host).compressed}]"
    elif len(host) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError
    if port is not None and not 1 <= port <= 65535:
        raise ValueError
    return f"https://{host}:{port or 443}"


def cluster_fingerprint(config: dict) -> str:
    server = server_identity(config["server"])
    pem = base64.b64decode(config["ca_data"], validate=True).decode("ascii")
    if not re.fullmatch(
        r"\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*", pem
    ):
        raise ValueError
    # SSL validates the certificate, while hashing DER ignores PEM line wrapping.
    ssl.create_default_context(cadata=pem)
    der = ssl.PEM_cert_to_DER_cert(pem.strip())
    return hashlib.sha256(f"{server}\n{hashlib.sha256(der).hexdigest()}".encode()).hexdigest()
