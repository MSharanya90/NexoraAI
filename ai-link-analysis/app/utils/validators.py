"""Security and validation utilities for SSRF prevention, URL hygiene, and DNS verification."""

import ipaddress
import socket
from typing import List, Optional, Tuple
from urllib.parse import urlparse, ParseResult


# Blocked hostnames and suffixes
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "instance-data",
}

BLOCKED_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".corp",
    ".home",
    ".onion",
    ".cluster.local",
)

# Explicit sensitive cloud metadata IPs
CLOUD_METADATA_IPS = {
    "169.254.169.254",   # AWS / GCP / Azure IMDS
    "100.100.100.200",   # Alibaba Cloud IMDS
    "fd00:ec2::254",     # AWS IPv6 IMDS
}

# Carrier-Grade NAT (RFC 6598: 100.64.0.0/10)
CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")
# Documentation and benchmark ranges
BENCHMARK_NET = ipaddress.ip_network("198.18.0.0/15")


class SSRFValidationError(Exception):
    """Raised when a URL or resolved IP violates SSRF security boundaries."""
    pass


def is_ip_address(hostname: str) -> bool:
    """Check if the string is a valid IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return True
    except ValueError:
        return False


def is_safe_ip(ip_str: str) -> Tuple[bool, Optional[str]]:
    """
    Check if an IP address string is safe for outbound requests.
    Returns (True, None) if safe, or (False, reason) if blocked.
    """
    try:
        ip = ipaddress.ip_address(ip_str.strip("[]"))
    except ValueError:
        return False, f"Invalid IP address format: {ip_str}"

    # Check explicit cloud metadata IPs
    if str(ip) in CLOUD_METADATA_IPS:
        return False, f"Access to cloud metadata IP ({ip}) is strictly forbidden"

    # Built-in python ipaddress checks
    if ip.is_loopback:
        return False, f"Loopback address ({ip}) is prohibited"
    if ip.is_private:
        return False, f"Private/Internal IP address ({ip}) is prohibited"
    if ip.is_link_local:
        return False, f"Link-local IP address ({ip}) is prohibited"
    if ip.is_multicast:
        return False, f"Multicast address ({ip}) is prohibited"
    if ip.is_reserved:
        return False, f"Reserved address ({ip}) is prohibited"
    if ip.is_unspecified:
        return False, f"Unspecified address ({ip}) is prohibited"

    # IPv4 specific checks
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in CARRIER_GRADE_NAT:
            return False, f"Carrier-Grade NAT address ({ip}) is prohibited"
        if ip in BENCHMARK_NET:
            return False, f"Benchmark test address ({ip}) is prohibited"

    # IPv6 6to4 and Teredo encapsulation check
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return is_safe_ip(str(ip.ipv4_mapped))
        if ip.is_site_local:
            return False, f"IPv6 site-local address ({ip}) is prohibited"

    return True, None


def resolve_and_validate_hostname(hostname: str) -> Tuple[bool, List[str], Optional[str]]:
    """
    Resolve DNS for a hostname and verify that every resolved IP is public and safe.
    Protects against DNS rebinding and dual-homed attacks.
    """
    clean_host = hostname.strip().lower().strip("[]")

    # Reject forbidden hostnames
    if clean_host in BLOCKED_HOSTNAMES:
        return False, [], f"Blocked sensitive hostname: '{clean_host}'"

    for suffix in BLOCKED_SUFFIXES:
        if clean_host.endswith(suffix):
            return False, [], f"Domain suffix '{suffix}' is prohibited for internal isolation"

    # If it's an IP literal, validate directly
    if is_ip_address(clean_host):
        is_safe, reason = is_safe_ip(clean_host)
        if not is_safe:
            return False, [], reason
        return True, [clean_host], None

    # Resolve hostname via standard DNS resolver
    resolved_ips: List[str] = []
    try:
        # Resolve both IPv4 and IPv6 addresses
        addr_info = socket.getaddrinfo(clean_host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for item in addr_info:
            sockaddr = item[4]
            ip = sockaddr[0]
            if ip not in resolved_ips:
                resolved_ips.append(ip)
    except socket.gaierror as e:
        return False, [], f"DNS resolution failed for '{clean_host}': {str(e)}"
    except Exception as e:
        return False, [], f"Unexpected DNS lookup error for '{clean_host}': {str(e)}"

    if not resolved_ips:
        return False, [], f"No IP addresses resolved for hostname '{clean_host}'"

    # Every single resolved IP MUST be safe to prevent dual-homed / DNS rebinding attacks
    for ip in resolved_ips:
        is_safe, reason = is_safe_ip(ip)
        if not is_safe:
            return False, resolved_ips, f"Hostname '{clean_host}' resolved to restricted IP {ip}: {reason}"

    return True, resolved_ips, None


def validate_target_url(url: str) -> Tuple[bool, Optional[str], Optional[ParseResult]]:
    """
    Comprehensive URL validation including schema, structure, and hostname security checks.
    """
    if not url or not isinstance(url, str):
        return False, "URL must be a non-empty string", None

    url = url.strip()

    # Reject absurdly long URLs
    if len(url) > 2048:
        return False, "URL exceeds maximum allowed length of 2048 characters", None

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Malformed URL: {str(e)}", None

    # Enforce HTTP / HTTPS scheme
    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"Unsupported URL scheme '{parsed.scheme}'. Only 'http' and 'https' are allowed.", None

    if not parsed.hostname:
        return False, "URL must contain a valid hostname or domain", None

    # Reject credentials in URL (e.g. http://user:pass@example.com)
    if parsed.username or parsed.password:
        return False, "URLs containing embedded user credentials are prohibited", None

    # Reject invalid ports
    if parsed.port:
        if parsed.port < 1 or parsed.port > 65535:
            return False, f"Invalid port number: {parsed.port}", None
        # Disallow well-known internal service ports like redis (6379), memcached (11211)
        RESTRICTED_PORTS = {22, 25, 110, 143, 6379, 11211, 27017, 9200, 2375, 2376}
        if parsed.port in RESTRICTED_PORTS:
            return False, f"Connection to restricted port {parsed.port} is blocked for security", None

    # Validate DNS & SSRF protection
    is_safe, resolved_ips, error_msg = resolve_and_validate_hostname(parsed.hostname)
    if not is_safe:
        return False, error_msg, None

    return True, None, parsed
