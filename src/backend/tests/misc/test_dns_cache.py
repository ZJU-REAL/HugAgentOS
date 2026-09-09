"""The desktop resolver cache: fast when the OS is slow, never sticky on failure."""

import socket
import time

import pytest

from core.config import local_mode
from core.infra import dns_cache


@pytest.fixture(autouse=True)
def restore():
    yield
    dns_cache.uninstall()


def test_repeat_lookups_hit_the_resolver_once(monkeypatch):
    calls = []

    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    assert dns_cache.install(ttl_s=60) is True
    first = socket.getaddrinfo("cloud.example", 443)
    assert socket.getaddrinfo("cloud.example", 443) == first
    assert socket.getaddrinfo("cloud.example", 443) == first
    assert calls == ["cloud.example"]


def test_a_different_target_is_not_served_from_another_answer(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))],
    )
    dns_cache.install(ttl_s=60)
    assert socket.getaddrinfo("a.example", 443)[0][4][0] == "a.example"
    assert socket.getaddrinfo("b.example", 443)[0][4][0] == "b.example"
    assert socket.getaddrinfo("a.example", 8443)[0][4][1] == 8443


def test_the_answer_expires_so_a_moved_host_is_picked_up(monkeypatch):
    answers = iter([[("first",)], [("second",)]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: next(answers))
    dns_cache.install(ttl_s=60)
    assert socket.getaddrinfo("cloud.example", 443) == [("first",)]
    monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + 3600)
    assert socket.getaddrinfo("cloud.example", 443) == [("second",)]


def test_a_failure_is_never_remembered(monkeypatch):
    outcomes = [socket.gaierror("temporarily unreachable"), [("recovered",)]]

    def resolver(*a, **k):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    dns_cache.install(ttl_s=60)
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("cloud.example", 443)
    assert socket.getaddrinfo("cloud.example", 443) == [("recovered",)]


def test_uninstall_restores_the_real_resolver(monkeypatch):
    original = socket.getaddrinfo
    dns_cache.install(ttl_s=60)
    assert socket.getaddrinfo is not original
    dns_cache.uninstall()
    assert socket.getaddrinfo is original
    assert dns_cache.installed() is False


def test_a_cloud_deployment_never_installs_it(monkeypatch):
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: False)
    assert local_mode.install_local_network_tuning() is False
    assert dns_cache.installed() is False


def test_the_desktop_local_backend_installs_it(monkeypatch):
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    assert local_mode.install_local_network_tuning() is True
    assert dns_cache.installed() is True
    assert local_mode.install_local_network_tuning() is False
