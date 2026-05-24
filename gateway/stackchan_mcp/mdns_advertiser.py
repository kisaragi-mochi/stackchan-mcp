"""mDNS/DNS-SD advertisement for the StackChan WebSocket gateway."""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_stackchan-mcp._tcp.local."
DEFAULT_INSTANCE = "stackchan-mcp"
SERVICE_NAME = f"{DEFAULT_INSTANCE}.{SERVICE_TYPE}"
FALLBACK_SERVICE_HOSTNAME = f"{DEFAULT_INSTANCE}.local."
TXT_VERSION = "1"


@dataclass(frozen=True)
class MdnsAdvertisement:
    """Resolved service advertisement parameters."""

    service_type: str
    service_name: str
    server: str
    port: int
    path: str
    properties: dict[str, str]
    parsed_addresses: list[str]


def _load_zeroconf_classes() -> tuple[type[Any], type[Any]]:
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf

    return AsyncZeroconf, ServiceInfo


def _is_usable_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        ip.version == 4
        and not ip.is_unspecified
        and not ip.is_loopback
        and not ip.is_multicast
    )


def _is_wildcard_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in {"", "*"}
    return ip.is_unspecified


def _build_service_hostname() -> str:
    label = socket.gethostname().split(".", 1)[0]
    safe_label = "".join(
        char.lower()
        if char.isascii() and (char.isalnum() or char == "-")
        else "-"
        for char in label
    ).strip("-")
    if not safe_label:
        return FALLBACK_SERVICE_HOSTNAME
    return f"{safe_label}.local."


def _iter_ifaddr_ipv4_addresses() -> list[str]:
    try:
        import ifaddr
    except ImportError:
        return []

    addresses: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            addresses.append(ip.ip)
    return addresses


def _iter_socket_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    hostnames = {socket.gethostname(), socket.getfqdn()}

    for hostname in hostnames:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        except socket.gaierror:
            continue
        for _family, _socktype, _proto, _canonname, sockaddr in infos:
            addresses.add(sockaddr[0])

    # Add the primary outbound IPv4 as a best-effort fallback. UDP connect()
    # selects a local address without sending packets.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    return sorted(addresses)


def _enumerate_usable_ipv4_addresses() -> list[str]:
    seen: set[str] = set()
    usable: list[str] = []
    for address in [*_iter_ifaddr_ipv4_addresses(), *_iter_socket_ipv4_addresses()]:
        if address in seen or not _is_usable_ipv4(address):
            continue
        seen.add(address)
        usable.append(address)
    return usable


def _resolve_concrete_host_ipv4_addresses(host: str) -> list[str]:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
        except socket.gaierror:
            return []
        addresses = [sockaddr[0] for *_unused, sockaddr in infos]
    else:
        addresses = [str(ip)]

    seen: set[str] = set()
    usable: list[str] = []
    for address in addresses:
        if address in seen or not _is_usable_ipv4(address):
            continue
        seen.add(address)
        usable.append(address)
    return usable


def build_advertisement(
    *,
    host: str,
    port: int,
    path: str = "/",
) -> MdnsAdvertisement | None:
    """Resolve advertisement parameters, or ``None`` if they would be unusable."""
    if port <= 0 or port > 65535:
        logger.warning(
            "mDNS advertisement skipped: WebSocket port %s is not publishable",
            port,
        )
        return None

    normalized_path = path if path.startswith("/") else f"/{path}"
    addresses = (
        _enumerate_usable_ipv4_addresses()
        if _is_wildcard_host(host)
        else _resolve_concrete_host_ipv4_addresses(host)
    )
    if not addresses:
        logger.warning(
            "mDNS advertisement skipped: no usable non-loopback IPv4 address "
            "found for HOST=%s",
            host,
        )
        return None

    return MdnsAdvertisement(
        service_type=SERVICE_TYPE,
        service_name=SERVICE_NAME,
        server=_build_service_hostname(),
        port=port,
        path=normalized_path,
        properties={"path": normalized_path, "version": TXT_VERSION},
        parsed_addresses=addresses,
    )


class MdnsAdvertiser:
    """Registers the gateway's WebSocket endpoint via mDNS/DNS-SD."""

    def __init__(self) -> None:
        self._zeroconf: Any | None = None
        self._service_info: Any | None = None

    async def start(self, *, host: str, port: int, path: str = "/") -> None:
        advertisement = build_advertisement(host=host, port=port, path=path)
        if advertisement is None:
            return

        AsyncZeroconf, ServiceInfo = _load_zeroconf_classes()
        zeroconf = AsyncZeroconf()
        info = ServiceInfo(
            advertisement.service_type,
            advertisement.service_name,
            port=advertisement.port,
            properties=advertisement.properties,
            server=advertisement.server,
            parsed_addresses=advertisement.parsed_addresses,
        )
        try:
            await zeroconf.async_register_service(info, allow_name_change=True)
        except Exception:
            await zeroconf.async_close()
            raise
        self._zeroconf = zeroconf
        self._service_info = info
        logger.info(
            "mDNS advertising %s on port %d with addresses %s",
            getattr(info, "name", advertisement.service_name),
            advertisement.port,
            ", ".join(advertisement.parsed_addresses),
        )

    async def stop(self) -> None:
        zeroconf = self._zeroconf
        info = self._service_info
        self._zeroconf = None
        self._service_info = None

        if zeroconf is None:
            return
        try:
            if info is not None:
                await zeroconf.async_unregister_service(info)
        finally:
            await zeroconf.async_close()
