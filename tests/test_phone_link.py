"""The addresses the kiosk offers a phone on the same network."""

import socket
import struct

import pytest

from openflight import phone_link as phone_link_module
from openflight.phone_link import lan_addresses, mdns_hostname, phone_link


@pytest.fixture
def network(monkeypatch):
    """Pin what the Pi's network looks like; tests change one part each."""

    def pin(route="192.168.1.23", interfaces=("192.168.1.23",), hostname="openflight"):
        monkeypatch.setattr(phone_link_module, "_route_address", lambda: route)
        monkeypatch.setattr(phone_link_module, "_interface_addresses", lambda: list(interfaces))
        monkeypatch.setattr(socket, "gethostname", lambda: hostname)

    pin()
    return pin


class FakeUdpSocket:
    """Stands in for the UDP socket used to ask the OS about a route."""

    def __init__(self, local_address=None, error=None):
        self.local_address = local_address
        self.error = error
        self.connected_to = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def connect(self, target):
        if self.error is not None:
            raise self.error
        self.connected_to = target

    def getsockname(self):
        return (self.local_address, 54321)

    def fileno(self):
        return 7


def ifreq_reply(address):
    """A struct ifreq as SIOCGIFADDR fills it: the address sits at bytes 20-24."""
    return struct.pack(
        "16sH2s4s232s", b"wlan0", socket.AF_INET, b"", socket.inet_aton(address), b""
    )


def test_a_pi_on_wifi_offers_its_address_then_its_local_name(network):
    assert phone_link("0.0.0.0", 8080) == {
        "available": True,
        "port": 8080,
        "urls": [
            {"kind": "ipv4", "host": "192.168.1.23", "url": "http://192.168.1.23:8080"},
            {"kind": "mdns", "host": "openflight.local", "url": "http://openflight.local:8080"},
        ],
    }


def test_the_port_the_server_runs_on_is_the_port_in_every_url(network):
    urls = [entry["url"] for entry in phone_link("0.0.0.0", 9090)["urls"]]

    assert urls == ["http://192.168.1.23:9090", "http://openflight.local:9090"]


def test_port_80_is_left_out_of_the_url(network):
    assert phone_link("0.0.0.0", 80)["urls"][0]["url"] == "http://192.168.1.23"


def test_no_network_gives_an_empty_list_and_says_so(network):
    network(route=None, interfaces=())

    assert phone_link("0.0.0.0", 8080) == {"available": False, "port": 8080, "urls": []}


def test_a_local_name_alone_is_not_offered_without_an_address(network):
    network(route=None, interfaces=(), hostname="openflight")

    assert phone_link("0.0.0.0", 8080)["urls"] == []


def test_an_unknown_port_gives_no_urls(network):
    assert phone_link("0.0.0.0", None) == {"available": False, "port": None, "urls": []}


def test_a_hotspot_with_no_route_out_is_still_found(network):
    network(route=None, interfaces=("127.0.0.1", "10.42.0.1"))

    assert lan_addresses("0.0.0.0") == ["10.42.0.1"]


def test_wifi_and_ethernet_are_both_listed_once_with_the_routed_one_first(network):
    network(route="192.168.1.23", interfaces=("127.0.0.1", "10.0.0.5", "192.168.1.23"))

    assert lan_addresses("0.0.0.0") == ["192.168.1.23", "10.0.0.5"]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "127.0.1.1", "169.254.10.20", "0.0.0.0", "224.0.0.251", "not-an-address", None],
)
def test_addresses_a_phone_cannot_reach_are_dropped(network, address):
    network(route=address, interfaces=(address,))

    assert lan_addresses("0.0.0.0") == []


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "::", "", None])
def test_a_server_bound_to_every_interface_offers_every_address(network, bind_host):
    assert lan_addresses(bind_host) == ["192.168.1.23"]


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "localhost"])
def test_a_server_bound_to_loopback_offers_nothing(network, monkeypatch, bind_host):
    monkeypatch.setattr(socket, "gethostbyname", lambda _host: "127.0.0.1")

    assert phone_link(bind_host, 8080) == {"available": False, "port": 8080, "urls": []}


def test_a_server_bound_to_one_address_offers_only_that_address(network, monkeypatch):
    network(route="192.168.1.23", interfaces=("192.168.1.23", "10.0.0.5"))
    monkeypatch.setattr(socket, "gethostbyname", lambda host: host)

    assert phone_link("10.0.0.5", 8080)["urls"] == [
        {"kind": "ipv4", "host": "10.0.0.5", "url": "http://10.0.0.5:8080"}
    ]


def test_a_bind_host_that_does_not_resolve_offers_nothing(network, monkeypatch):
    def fail(_host):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "gethostbyname", fail)

    assert lan_addresses("nowhere.invalid") == []


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("openflight", "openflight.local"),
        ("RaspberryPi", "raspberrypi.local"),
        ("pi-5.lan", "pi-5.local"),
        ("localhost", None),
        ("", None),
        ("bad_name", None),
        ("-leading", None),
    ],
)
def test_the_local_name_comes_from_the_hostname(monkeypatch, hostname, expected):
    monkeypatch.setattr(socket, "gethostname", lambda: hostname)

    assert mdns_hostname() == expected


def test_a_hostname_lookup_that_fails_gives_no_local_name(monkeypatch):
    def fail():
        raise OSError("no hostname")

    monkeypatch.setattr(socket, "gethostname", fail)

    assert mdns_hostname() is None


def test_the_route_probe_reads_the_local_address_without_sending(monkeypatch):
    probe = FakeUdpSocket(local_address="192.168.1.23")
    monkeypatch.setattr(socket, "socket", lambda *_args: probe)

    assert phone_link_module._route_address() == "192.168.1.23"
    assert probe.connected_to == ("8.8.8.8", 80)


def test_the_route_probe_gives_nothing_when_the_network_is_unreachable(monkeypatch):
    probe = FakeUdpSocket(error=OSError(101, "Network is unreachable"))
    monkeypatch.setattr(socket, "socket", lambda *_args: probe)

    assert phone_link_module._route_address() is None


def test_the_route_probe_gives_nothing_when_no_socket_can_be_opened(monkeypatch):
    def fail(*_args):
        raise OSError("too many open files")

    monkeypatch.setattr(socket, "socket", fail)

    assert phone_link_module._route_address() is None
    assert phone_link_module._interface_addresses() == []


def test_interfaces_are_read_on_linux_and_ones_without_an_address_skipped(monkeypatch):
    replies = {b"lo": ifreq_reply("127.0.0.1"), b"wlan0": ifreq_reply("192.168.1.23")}

    def ioctl(_fileno, request, ifreq):
        assert request == 0x8915
        name = ifreq.rstrip(b"\x00")
        if name not in replies:
            raise OSError(99, "Cannot assign requested address")
        return replies[name]

    monkeypatch.setattr(phone_link_module.sys, "platform", "linux")
    monkeypatch.setattr(socket, "socket", lambda *_args: FakeUdpSocket())
    monkeypatch.setattr(socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0"), (3, "wlan0")])
    monkeypatch.setattr(phone_link_module.fcntl, "ioctl", ioctl)

    assert phone_link_module._interface_addresses() == ["127.0.0.1", "192.168.1.23"]


def test_interfaces_are_not_read_where_the_ioctl_differs(monkeypatch):
    monkeypatch.setattr(phone_link_module.sys, "platform", "darwin")

    def fail():
        raise AssertionError("must not list interfaces")

    monkeypatch.setattr(socket, "if_nameindex", fail)

    assert phone_link_module._interface_addresses() == []


def test_a_failed_interface_listing_gives_nothing(monkeypatch):
    def fail():
        raise OSError("no interfaces")

    monkeypatch.setattr(phone_link_module.sys, "platform", "linux")
    monkeypatch.setattr(socket, "socket", lambda *_args: FakeUdpSocket())
    monkeypatch.setattr(socket, "if_nameindex", fail)

    assert phone_link_module._interface_addresses() == []


def test_nothing_raises_on_this_machine_whatever_its_network_is():
    result = phone_link("0.0.0.0", 8080)

    assert result["available"] is bool(result["urls"])
    assert all(entry["url"].startswith("http://") for entry in result["urls"])
