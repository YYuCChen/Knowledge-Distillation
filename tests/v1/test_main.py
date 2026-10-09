from knowledge_distiller.v1.file_lock import acquire
import socket
from types import SimpleNamespace

import pytest

from knowledge_distiller import __main__ as cli


@pytest.mark.parametrize("args", [["--help"], ["--port", "0"], ["--port", "invalid"]])
def test_cli_validates_before_creating_application(monkeypatch, args):
    def unexpected(*_):
        pytest.fail("argument handling must not start the application")

    monkeypatch.setattr(cli, "create_application", unexpected)
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == (0 if args == ["--help"] else 2)


def test_occupied_port_does_not_create_database(tmp_path):
    root = tmp_path / "data"
    with socket.socket() as listener:
        listener.bind((cli.DEFAULT_HOST, 0))
        listener.listen()
        with pytest.raises(SystemExit) as result:
            cli.main(["--port", str(listener.getsockname()[1]), "--data-dir", str(root)])
    assert result.value.code == 2
    assert not root.exists()


def test_data_directory_lock_blocks_second_worker(tmp_path, monkeypatch):
    def unexpected(*_):
        pytest.fail("locked data directory must not start another worker")

    monkeypatch.setattr(cli, "create_application", unexpected)
    with socket.socket() as probe:
        probe.bind((cli.DEFAULT_HOST, 0))
        port = probe.getsockname()[1]
    with acquire(tmp_path / '.instance.lock'):
        with pytest.raises(SystemExit) as result:
            cli.main(["--port", str(port), "--data-dir", str(tmp_path)])
    assert result.value.code == 2


def test_worker_stops_and_lock_releases_when_server_fails(tmp_path, monkeypatch):
    stopped = []

    def factory(paths):
        assert paths.data_root == tmp_path.resolve()

        def fail(**kwargs):
            assert kwargs["host"] == "127.0.0.1"
            assert kwargs["use_reloader"] is False
            raise RuntimeError("server failure")

        return SimpleNamespace(run=fail, config={
            "KNOWLEDGE_DISTILLER_CLOSE_FEISHU": lambda: stopped.append("feishu"),
            "KNOWLEDGE_DISTILLER_WORKER": SimpleNamespace(stop=lambda: stopped.append(True)),
            "KNOWLEDGE_DISTILLER_CLOSE_BROWSERS": lambda: stopped.append("browsers"),
        })

    monkeypatch.setattr(cli, "create_application", factory)
    with socket.socket() as probe:
        probe.bind((cli.DEFAULT_HOST, 0))
        port = probe.getsockname()[1]
    with pytest.raises(RuntimeError, match="server failure"):
        cli.main(["--port", str(port), "--data-dir", str(tmp_path)])
    assert stopped == ["feishu", True, "browsers"]
    with acquire(tmp_path / '.instance.lock'):
        pass
