"""The endpoint the kiosk asks for the address a phone should open."""

import socket

import pytest

from openflight import phone_link as phone_link_module, server as server_module


@pytest.fixture
def client():
    return server_module.app.test_client()


@pytest.fixture
def on_wifi(monkeypatch):
    monkeypatch.setattr(phone_link_module, "_route_address", lambda: "192.168.1.23")
    monkeypatch.setattr(phone_link_module, "_interface_addresses", lambda: ["192.168.1.23"])
    monkeypatch.setattr(socket, "gethostname", lambda: "openflight")


def test_phone_link_uses_the_port_the_server_is_running_on(client, on_wifi):
    response = client.get("/api/phone-link", base_url="http://0.0.0.0:9090")

    assert response.status_code == 200
    assert response.get_json() == {
        "available": True,
        "port": 9090,
        "urls": [
            {"kind": "ipv4", "host": "192.168.1.23", "url": "http://192.168.1.23:9090"},
            {"kind": "mdns", "host": "openflight.local", "url": "http://openflight.local:9090"},
        ],
    }


def test_phone_link_with_no_network_is_an_empty_list_not_an_error(client, monkeypatch):
    monkeypatch.setattr(phone_link_module, "_route_address", lambda: None)
    monkeypatch.setattr(phone_link_module, "_interface_addresses", lambda: [])

    response = client.get("/api/phone-link", base_url="http://0.0.0.0:8080")

    assert response.status_code == 200
    assert response.get_json() == {"available": False, "port": 8080, "urls": []}


def test_phone_link_offers_nothing_when_the_server_only_listens_on_loopback(client, on_wifi):
    response = client.get("/api/phone-link", base_url="http://127.0.0.1:8080")

    assert response.get_json() == {"available": False, "port": 8080, "urls": []}


def test_phone_link_offers_nothing_when_the_port_is_unknown(client, on_wifi):
    """A server on a Unix socket has no TCP port to put in a URL."""
    response = client.get("/api/phone-link", environ_overrides={"SERVER_PORT": ""})

    assert response.status_code == 200
    assert response.get_json() == {"available": False, "port": None, "urls": []}


def test_phone_link_is_read_only(client):
    assert client.post("/api/phone-link").status_code == 405
