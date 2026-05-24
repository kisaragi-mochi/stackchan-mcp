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

# Private (RFC1918) IPv4 ranges. Addresses inside these ranges are the most
# likely to be reachable from a same-LAN device, so they are advertised first.
# Anything outside is still advertised, just ordered after these.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)

# Only prefixes shorter than /31 have a distinct network and broadcast address.
# A /31 (point-to-point) or /32 (host) address must never be treated as a
# network or broadcast address, since that would drop a legitimate host IP.
_MAX_NETWORK_BROADCAST_PREFIX = 30


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


def _is_network_or_broadcast_address(address: str, prefix: int | None) -> bool:
    """Return ``True`` when ``address`` is the subnet network or broadcast address.

    Requires the subnet prefix: without it (e.g. the socket-based source) the
    network/broadcast endpoints cannot be derived, so callers pass ``None`` and
    the address is kept. Host prefixes (/31, /32) have no distinct network or
    broadcast address and are never excluded here.
    """
    if prefix is None or prefix > _MAX_NETWORK_BROADCAST_PREFIX:
        return False
    try:
        interface = ipaddress.ip_interface(f"{address}/{prefix}")
    except ValueError:
        return False
    network = interface.network
    return interface.ip in (network.network_address, network.broadcast_address)


def _is_private_lan_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in _PRIVATE_NETWORKS)


def _lan_reachability_tier(address: str) -> int:
    """Sort key: private (RFC1918) LAN addresses first, everything else after.

    Returns a 2-value tier so a stable sort preserves the original enumeration
    order within each tier.
    """
    return 0 if _is_private_lan_address(address) else 1


def _select_advertised_addresses(
    candidates: list[tuple[str, int | None]],
) -> list[str]:
    """Filter, de-duplicate and order candidate addresses for advertisement.

    Excludes only clearly-unusable addresses (loopback/multicast/unspecified via
    :func:`_is_usable_ipv4`, plus network/broadcast addresses when a prefix is
    known). All remaining addresses are kept and ordered by LAN-reachability
    likelihood (RFC1918 private ranges first) using a stable sort, so addresses
    a same-LAN device can reach are tried before overlay/global/edge-case ones.
    """
    seen: set[str] = set()
    usable: list[str] = []
    for address, prefix in candidates:
        if address in seen or not _is_usable_ipv4(address):
            continue
        if _is_network_or_broadcast_address(address, prefix):
            continue
        seen.add(address)
        usable.append(address)
    usable.sort(key=_lan_reachability_tier)
    return usable


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


def _iter_ifaddr_ipv4_addresses() -> list[tuple[str, int | None]]:
    """Enumerate host IPv4 addresses with their subnet prefix length.

    The prefix (``ip.network_prefix``) lets the caller drop network/broadcast
    addresses; it is ``None`` only if ifaddr reports a non-integer prefix.
    """
    try:
        import ifaddr
    except ImportError:
        return []

    addresses: list[tuple[str, int | None]] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            prefix = ip.network_prefix if isinstance(ip.network_prefix, int) else None
            addresses.append((ip.ip, prefix))
    return addresses


def _iter_socket_ipv4_addresses() -> list[tuple[str, int | None]]:
    """Enumerate host IPv4 addresses via socket resolution.

    This source carries no subnet prefix, so each entry pairs the address with
    ``None``; network/broadcast addresses therefore cannot be (and are not)
    excluded from this source.
    """
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

    return [(address, None) for address in sorted(addresses)]


def _enumerate_usable_ipv4_addresses() -> list[str]:
    return _select_advertised_addresses(
        [*_iter_ifaddr_ipv4_addresses(), *_iter_socket_ipv4_addresses()]
    )


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

    # A concrete HOST carries no subnet prefix, so network/broadcast addresses
    # cannot be derived; pair each address with ``None`` to keep them.
    return _select_advertised_addresses([(address, None) for address in addresses])


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
