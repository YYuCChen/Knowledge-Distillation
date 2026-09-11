import json
import socket

import pytest

from knowledge_distiller.v1.local_address import (
    LocalAddress, LocalAddressError, allowed_host, load, port_available, save, validate,
)


@pytest.mark.parametrize('name', ['', '-mine', 'mine-', 'mine.localhost', 'http://mine', 'mine/path', '含中文'])
def test_local_address_rejects_unsafe_names(name):
    with pytest.raises(LocalAddressError, match='local_address_name_invalid'):
        validate(name, '57740')


@pytest.mark.parametrize('port', ['', '0', '80', '65536', '57.740', '１２３４'])
def test_local_address_rejects_unsafe_ports(port):
    with pytest.raises(LocalAddressError, match='local_address_port_invalid'):
        validate('mine', port)


def test_local_address_persists_outside_database_and_normalizes_name(tmp_path):
    address = validate('My-Knowledge', '58888')
    save(tmp_path, address)
    assert load(tmp_path) == LocalAddress('my-knowledge', 58888)
    assert json.loads((tmp_path/'local-address.json').read_text()) == {'name':'my-knowledge','port':58888}


def test_local_address_defaults_for_existing_users(tmp_path):
    assert load(tmp_path) == LocalAddress()


def test_fixed_port_conflict_is_detected_without_random_fallback():
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    try:
        assert not port_available(occupied.getsockname()[1])
    finally:
        occupied.close()


def test_only_configured_local_hosts_and_port_are_allowed():
    address = LocalAddress('mine', 57740)
    assert allowed_host('mine.localhost:57740', address)
    assert allowed_host('127.0.0.1:57740', address)
    assert allowed_host('localhost:57740', address)
    assert not allowed_host('other.localhost:57740', address)
    assert not allowed_host('mine.localhost:57741', address)
    assert not allowed_host('evil.example:57740', address)
