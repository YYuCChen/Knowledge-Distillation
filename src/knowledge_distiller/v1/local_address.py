"""Persistent, pre-database configuration for the desktop web address."""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_NAME = "knowledge-distiller"
DEFAULT_PORT = 57740
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class LocalAddressError(ValueError):
    pass


class LocalAddressBindError(OSError):
    pass


@dataclass(frozen=True)
class LocalAddress:
    name: str = DEFAULT_NAME
    port: int = DEFAULT_PORT

    @property
    def host(self) -> str:
        return f"{self.name}.localhost"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


def validate(name: str, port: int | str) -> LocalAddress:
    normalized = name.strip().lower() if isinstance(name, str) else ''
    if not _LABEL.fullmatch(normalized):
        raise LocalAddressError("local_address_name_invalid")
    if type(port) is str:
        if not port.isascii() or not port.isdecimal():
            raise LocalAddressError("local_address_port_invalid")
        port = int(port)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise LocalAddressError("local_address_port_invalid")
    return LocalAddress(normalized, port)


def config_path(data_root: Path) -> Path:
    return data_root / "local-address.json"


def load(data_root: Path) -> LocalAddress:
    path = config_path(data_root)
    if not path.is_file():
        return LocalAddress()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return validate(value.get("name", ""), value.get("port", ""))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise LocalAddressError("local_address_config_invalid") from error


def save(data_root: Path, address: LocalAddress) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    path = config_path(data_root)
    descriptor, temporary = tempfile.mkstemp(prefix=".local-address-", dir=data_root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"name": address.name, "port": address.port}, stream,
                      ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def port_available(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def allowed_host(host: str, address: LocalAddress, *, actual_port: int | None = None) -> bool:
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit('http://' + host.lower())
        hostname, port = parsed.hostname, parsed.port or 80
    except (ValueError, TypeError):
        return False
    expected_port = actual_port or address.port
    return (not parsed.username and not parsed.password and not parsed.path
            and not parsed.query and not parsed.fragment
            and port == expected_port and hostname in {address.host, "localhost", "127.0.0.1"})


def install_boundary(app, address, port):
    """Freeze trusted hosts at bind time; a pending configuration is not live yet."""
    from flask import request
    app.config['LOCAL_ADDRESS_ACTIVE'] = address
    app.config['LOCAL_ADDRESS_PORT'] = port

    @app.before_request
    def check_local_request():
        if not allowed_host(request.host, address, actual_port=port):
            return '本地访问地址不匹配。', 403
        origin = request.headers.get('Origin')
        if origin and origin != 'http://' + request.host:
            return '请求来源不匹配。', 403
