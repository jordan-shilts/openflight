"""Addresses a phone on the same network can open to reach the kiosk UI.

The kiosk shows these as a QR code. Everything here is standard library and
none of it raises: a Pi with no network answers with an empty list.
"""

import fcntl
import ipaddress
import re
import socket
import struct
import sys

WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})
DEFAULT_HTTP_PORT = 80

# connect() on a UDP socket sends nothing. It only asks the OS which local
# address it would use to reach the target, which is the Pi's LAN address.
_ROUTE_PROBE_TARGET = ("8.8.8.8", 80)
# Linux ioctl that reads one interface's IPv4 address into a struct ifreq.
_SIOCGIFADDR = 0x8915
_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _route_address() -> str | None:
    """The local address on the default route, or None when there is no route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_ROUTE_PROBE_TARGET)
            return probe.getsockname()[0]
    except OSError:
        return None


def _interface_address(query: socket.socket, name: str) -> str | None:
    """One interface's IPv4 address, or None when it is down or has none."""
    request = struct.pack("256s", name.encode()[:15])
    try:
        reply = fcntl.ioctl(query.fileno(), _SIOCGIFADDR, request)
    except OSError:
        return None
    return socket.inet_ntoa(reply[20:24])


def _interface_addresses() -> list[str]:
    """Every interface's IPv4 address (Linux only).

    Finds a LAN that has no route out, such as the Pi's own Wi-Fi hotspot.
    """
    if not sys.platform.startswith("linux"):
        return []
    addresses = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as query:
            for _index, name in socket.if_nameindex():
                address = _interface_address(query, name)
                if address is not None:
                    addresses.append(address)
    except OSError:
        return []
    return addresses


def _resolve(host: str) -> str | None:
    """The IPv4 address a specific bind host stands for."""
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return None


def _is_lan_address(address: str | None) -> bool:
    """True for an IPv4 address another device on the network can connect to."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not (
        parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast
    )


def _is_wildcard(bind_host: str | None) -> bool:
    return bind_host is None or bind_host in WILDCARD_HOSTS


def lan_addresses(bind_host: str | None) -> list[str]:
    """IPv4 addresses the server answers on, the default route's first.

    A server bound to one address is only reachable there; one bound to
    loopback is not reachable from a phone at all.
    """
    if _is_wildcard(bind_host):
        candidates = [_route_address(), *_interface_addresses()]
    else:
        candidates = [_resolve(bind_host)]
    found: list[str] = []
    for candidate in candidates:
        if _is_lan_address(candidate) and candidate not in found:
            found.append(candidate)
    return found


def mdns_hostname() -> str | None:
    """``<hostname>.local``, the name Raspberry Pi OS announces over mDNS."""
    try:
        label = socket.gethostname().split(".")[0].lower()
    except OSError:
        return None
    if label == "localhost" or not _HOST_LABEL.fullmatch(label):
        return None
    return f"{label}.local"


def _url(host: str, port: int) -> str:
    return f"http://{host}" if port == DEFAULT_HTTP_PORT else f"http://{host}:{port}"


def phone_link(bind_host: str | None, port: int | None) -> dict:
    """What ``GET /api/phone-link`` returns.

    ``urls`` lists IPv4 addresses first because ``.local`` names do not resolve
    on every network. ``available`` is False, and ``urls`` empty, when the Pi
    has no address a phone could reach.
    """
    if port is None:
        return {"available": False, "port": None, "urls": []}
    addresses = lan_addresses(bind_host)
    urls = [{"kind": "ipv4", "host": host, "url": _url(host, port)} for host in addresses]
    hostname = mdns_hostname() if addresses and _is_wildcard(bind_host) else None
    if hostname is not None:
        urls.append({"kind": "mdns", "host": hostname, "url": _url(hostname, port)})
    return {"available": bool(urls), "port": port, "urls": urls}
