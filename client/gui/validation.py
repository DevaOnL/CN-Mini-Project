"""Shared GUI input validation helpers."""

import ipaddress
import re

HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
NUMERIC_IPV4_RE = re.compile(r"^(?:\d+\.){3}\d+$")
MISTYPED_IPV4_SUFFIX_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)\.(\d+)([A-Za-z][A-Za-z0-9-]*)$"
)


def suggested_ipv4_correction(host: str) -> str | None:
    host = host.strip()
    match = MISTYPED_IPV4_SUFFIX_RE.fullmatch(host)
    if match is None:
        return None

    candidate = ".".join(match.group(index) for index in range(1, 5))
    try:
        ipaddress.IPv4Address(candidate)
    except ipaddress.AddressValueError:
        return None
    return candidate


def is_valid_host(host: str) -> bool:
    host = host.strip()
    if not host or len(host) > 253:
        return False

    if NUMERIC_IPV4_RE.match(host):
        try:
            ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            return False
        return True

    if suggested_ipv4_correction(host) is not None:
        return False

    if ":" in host or host.startswith("[") or host.endswith("]"):
        return False

    labels = host.split(".")
    return all(label and HOSTNAME_LABEL_RE.fullmatch(label) for label in labels)
