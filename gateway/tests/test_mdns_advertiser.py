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
        lambda: ["127.0.0.1", "192.0.2.10", "0.0.0.0"],
    )
    monkeypatch.setattr(
        mdns,
        "_iter_socket_ipv4_addresses",
        lambda: ["192.0.2.10", "10.0.0.5"],
    )

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    assert advertisement.parsed_addresses == ["192.0.2.10", "10.0.0.5"]


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

