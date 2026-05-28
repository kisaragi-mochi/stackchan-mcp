"""Tests for mDNS/DNS-SD gateway advertisement."""

from __future__ import annotations

import pytest

from stackchan_mcp import mdns_advertiser as mdns
from stackchan_mcp.mdns_advertiser import MdnsAdvertiser, build_advertisement


def test_service_type_and_txt_defaults() -> None:
    advertisement = build_advertisement(
        host="192.0.2.10",
        port=8765,
        path="/",
    )

    assert advertisement is not None
    assert advertisement.service_type == "_stackchan-mcp._tcp.local."
    assert advertisement.service_name == "stackchan-mcp._stackchan-mcp._tcp.local."
    assert advertisement.port == 8765
    assert advertisement.properties == {"path": "/", "version": "1"}
    assert advertisement.parsed_addresses == ["192.0.2.10"]


def test_wildcard_host_advertises_all_usable_non_loopback_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mdns,
        "_iter_ifaddr_ipv4_addresses",
        lambda: [("127.0.0.1", 8), ("192.0.2.10", 24), ("0.0.0.0", 24)],
    )
    monkeypatch.setattr(
        mdns,
        "_iter_socket_ipv4_addresses",
        lambda: [("192.0.2.10", None), ("10.0.0.5", None)],
    )

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    # 10.0.0.5 is RFC1918 (tier 1) and sorts ahead of the public 192.0.2.10.
    assert advertisement.parsed_addresses == ["10.0.0.5", "192.0.2.10"]


def test_rfc1918_addresses_are_advertised_before_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mdns,
        "_iter_ifaddr_ipv4_addresses",
        lambda: [
            ("203.0.113.7", 24),  # public, tier 2
            ("192.168.0.10", 24),  # RFC1918, tier 1
            ("10.1.2.3", 8),  # RFC1918, tier 1
            ("172.16.5.6", 12),  # RFC1918, tier 1
        ],
    )
    monkeypatch.setattr(mdns, "_iter_socket_ipv4_addresses", lambda: [])

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    # All kept; the three private addresses move ahead of the public one, and
    # within each tier the original enumeration order is preserved.
    assert advertisement.parsed_addresses == [
        "192.168.0.10",
        "10.1.2.3",
        "172.16.5.6",
        "203.0.113.7",
    ]


def test_cgnat_address_is_kept_but_ordered_after_rfc1918(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reproduces the real-device scenario from the issue: a CGNAT address
    # (100.64.0.0/10, here as a /32 host address) must remain advertised but be
    # tried only after the reachable LAN address.
    monkeypatch.setattr(
        mdns,
        "_iter_ifaddr_ipv4_addresses",
        lambda: [
            ("100.64.10.20", 32),  # CGNAT, tier 2 (must NOT be dropped)
            ("192.168.0.10", 24),  # RFC1918 LAN, tier 1
        ],
    )
    monkeypatch.setattr(mdns, "_iter_socket_ipv4_addresses", lambda: [])

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    assert advertisement.parsed_addresses == ["192.168.0.10", "100.64.10.20"]


def test_network_and_broadcast_addresses_are_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mdns,
        "_iter_ifaddr_ipv4_addresses",
        lambda: [
            ("192.168.0.0", 24),  # network address -> excluded
            ("192.168.0.255", 24),  # broadcast address -> excluded
            ("192.168.0.10", 24),  # host address -> kept
        ],
    )
    monkeypatch.setattr(mdns, "_iter_socket_ipv4_addresses", lambda: [])

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    assert advertisement.parsed_addresses == ["192.168.0.10"]


def test_host_prefixes_are_not_treated_as_network_or_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /31 and /32 have no distinct network/broadcast address; a legitimate host
    # IP on such a prefix must not be dropped.
    monkeypatch.setattr(
        mdns,
        "_iter_ifaddr_ipv4_addresses",
        lambda: [("192.168.5.0", 32), ("10.0.0.0", 31)],
    )
    monkeypatch.setattr(mdns, "_iter_socket_ipv4_addresses", lambda: [])

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    assert advertisement.parsed_addresses == ["192.168.5.0", "10.0.0.0"]


def test_addresses_without_prefix_are_not_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The socket source carries no prefix; a ".0"-looking address from it cannot
    # be classified as a network address and must be kept (zero-regression).
    monkeypatch.setattr(mdns, "_iter_ifaddr_ipv4_addresses", lambda: [])
    monkeypatch.setattr(
        mdns,
        "_iter_socket_ipv4_addresses",
        lambda: [("192.168.0.0", None), ("192.168.0.10", None)],
    )

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    assert advertisement.parsed_addresses == ["192.168.0.0", "192.168.0.10"]


def test_mixed_tier_ordering_preserves_within_tier_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ifaddr source (with prefixes) is enumerated before the socket source; the
    # stable sort must keep that combined order within each tier.
    monkeypatch.setattr(
        mdns,
        "_iter_ifaddr_ipv4_addresses",
        lambda: [("198.51.100.4", 24), ("192.168.1.2", 24)],
    )
    monkeypatch.setattr(
        mdns,
        "_iter_socket_ipv4_addresses",
        lambda: [("203.0.113.9", None), ("10.5.5.5", None)],
    )

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    # tier 1 (private), original order: 192.168.1.2 then 10.5.5.5;
    # tier 2 (other), original order: 198.51.100.4 then 203.0.113.9.
    assert advertisement.parsed_addresses == [
        "192.168.1.2",
        "10.5.5.5",
        "198.51.100.4",
        "203.0.113.9",
    ]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_not_advertised(host: str) -> None:
    assert build_advertisement(host=host, port=8765) is None


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_unpublishable_ports_are_not_advertised(port: int) -> None:
    assert build_advertisement(host="192.0.2.10", port=port) is None


@pytest.mark.asyncio
async def test_advertiser_registers_service(monkeypatch: pytest.MonkeyPatch) -> None:
    instances: list[FakeAsyncZeroconf] = []

    class FakeServiceInfo:
        def __init__(self, service_type: str, service_name: str, **kwargs) -> None:
            self.type = service_type
            self.name = service_name
            self.kwargs = kwargs

    class FakeAsyncZeroconf:
        def __init__(self) -> None:
            self.registered = []
            self.unregistered = []
            self.closed = False
            instances.append(self)

        async def async_register_service(
            self, info: FakeServiceInfo, *, allow_name_change: bool = False
        ) -> None:
            self.registered.append((info, allow_name_change))

        async def async_unregister_service(self, info: FakeServiceInfo) -> None:
            self.unregistered.append(info)

        async def async_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(mdns, "_load_zeroconf_classes", lambda: (FakeAsyncZeroconf, FakeServiceInfo))
    monkeypatch.setattr(mdns, "_enumerate_usable_ipv4_addresses", lambda: ["192.0.2.10", "10.0.0.5"])

    advertiser = MdnsAdvertiser()
    await advertiser.start(host="0.0.0.0", port=8765, path="/")

    assert len(instances) == 1
    zeroconf = instances[0]
    assert len(zeroconf.registered) == 1
    info, allow_name_change = zeroconf.registered[0]
    assert allow_name_change is True
    assert info.type == "_stackchan-mcp._tcp.local."
    assert info.name == "stackchan-mcp._stackchan-mcp._tcp.local."
    assert info.kwargs["port"] == 8765
    assert info.kwargs["properties"] == {"path": "/", "version": "1"}
    assert info.kwargs["parsed_addresses"] == ["192.0.2.10", "10.0.0.5"]

    await advertiser.stop()

    assert zeroconf.unregistered == [info]
    assert zeroconf.closed is True

@pytest.mark.asyncio
async def test_advertiser_closes_zeroconf_when_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[FailingAsyncZeroconf] = []

    class FakeServiceInfo:
        def __init__(self, service_type: str, service_name: str, **kwargs) -> None:
            self.type = service_type
            self.name = service_name
            self.kwargs = kwargs

    class FailingAsyncZeroconf:
        def __init__(self) -> None:
            self.closed = False
            instances.append(self)

        async def async_register_service(
            self, info: FakeServiceInfo, *, allow_name_change: bool = False
        ) -> None:
            raise RuntimeError("mock registration failure")

        async def async_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        mdns,
        "_load_zeroconf_classes",
        lambda: (FailingAsyncZeroconf, FakeServiceInfo),
    )

    advertiser = MdnsAdvertiser()
    with pytest.raises(RuntimeError, match="mock registration failure"):
        await advertiser.start(host="192.0.2.10", port=8765, path="/")

    assert len(instances) == 1
    assert instances[0].closed is True

