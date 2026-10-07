"""Trusted execution boundary for the source and frozen Vault kit."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
from typing import Iterable

from .adapters.python_policy import PYTHON_VERSION
from .wiki_kit import KitManifest, WikiKitError, verify_source_kit
from .wiki_lock import WikiLockError, canonical_vault


class WikiKitRuntimeError(RuntimeError):
    """A fixed-code trusted-runtime failure."""


@dataclass(frozen=True)
class KitExecution:
    returncode: int
    stdout: str


def bundled_kit_root() -> Path:
    if getattr(sys, "frozen", False):
        try:
            pyinstaller_root = Path(getattr(sys, "_MEIPASS")).resolve(strict=True)
            root = (Path(getattr(sys, "_MEIPASS")) / "vault-kit").resolve(strict=True)
            boundary = (
                Path(sys.executable).resolve(strict=True).parents[1]
                if sys.platform == "darwin" else pyinstaller_root
            )
        except (AttributeError, IndexError, OSError) as error:
            raise WikiKitRuntimeError("kit_read_failed") from error
        # PyInstaller's macOS bundle links Contents/Frameworks/vault-kit to
        # Contents/Resources/vault-kit. Resolve that application-owned link,
        # but never accept a resource that escapes the signed bundle.
        if root == boundary or not root.is_relative_to(boundary):
            raise WikiKitRuntimeError("kit_symlink")
        return root
    return Path(__file__).resolve().parents[3] / "vault-kit"


class WikiKitRuntime:
    """Build commands only from a verified, application-owned kit."""

    def __init__(self, kit_root: Path | str | None = None, *,
                 python_executable: Path | str | None = None,
                 frozen_executable: Path | str | None = None):
        self.kit_root = Path(kit_root) if kit_root is not None else bundled_kit_root()
        self.python_executable = Path(python_executable or sys.executable)
        self.frozen_executable = Path(frozen_executable) if frozen_executable else None
        self._python_verified = False

    @property
    def frozen(self) -> bool:
        return self.frozen_executable is not None or bool(getattr(sys, "frozen", False))

    def verify(self) -> KitManifest:
        try:
            manifest = verify_source_kit(self.kit_root)
        except WikiKitError as error:
            raise WikiKitRuntimeError(str(error)) from error
        if manifest.protocol_version != 2:
            raise WikiKitRuntimeError("kit_incompatible")
        if not self.frozen and not self._python_verified:
            try:
                result = subprocess.run(
                    [os.fspath(self.python_executable), "-E", "-B", "-c",
                     "import json,platform;print(json.dumps([platform.python_implementation(),platform.python_version()]))"],
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    capture_output=True, text=True, encoding="utf-8", timeout=10,
                    check=False,
                )
                record = json.loads(result.stdout)
            except (OSError, UnicodeError, subprocess.TimeoutExpired,
                    json.JSONDecodeError) as error:
                raise WikiKitRuntimeError("runner_unavailable") from error
            if result.returncode != 0 or record != ["CPython", PYTHON_VERSION]:
                raise WikiKitRuntimeError("runner_unavailable")
            self._python_verified = True
        return manifest

    def command(self, tool: str, root: Path | str,
                arguments: Iterable[str] = ()) -> tuple[str, ...]:
        if tool not in {"kb", "session", "display"}:
            raise WikiKitRuntimeError("kit_incompatible")
        self.verify()
        try:
            canonical_vault(root)
        except WikiLockError as error:
            raise WikiKitRuntimeError("raw_path_invalid") from error
        args = tuple(str(value) for value in arguments)
        if any("\x00" in value for value in args):
            raise WikiKitRuntimeError("kit_incompatible")
        if self.frozen:
            executable = self.frozen_executable or Path(sys.executable)
            return (os.fspath(executable), "--wiki-kit", tool, "--vault-root",
                    os.fspath(root), "--", *args)
        scripts = {
            "kb": "kb.py",
            "session": "wiki_session.py",
            "display": "wiki_display.py",
        }
        script = self.kit_root / "tools" / scripts[tool]
        if tool in {"kb", "display"}:
            return (os.fspath(self.python_executable), "-E", "-B", os.fspath(script),
                    *args, "--root", os.fspath(root))
        return (os.fspath(self.python_executable), "-E", "-B", os.fspath(script),
                "--root", os.fspath(root), *args)

    def shell_command(self, tool: str, root: Path | str,
                      arguments: Iterable[str] = ()) -> str:
        return shlex.join(self.command(tool, root, arguments))

    def run(self, tool: str, root: Path | str, arguments: Iterable[str] = (), *,
            session_environment: dict[str, str] | None = None,
            timeout: float = 120) -> KitExecution:
        environment = {
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
        }
        if session_environment:
            environment.update(session_environment)
        try:
            result = subprocess.run(
                self.command(tool, root, arguments), cwd=root, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                check=False,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
            raise WikiKitRuntimeError("protocol_error") from error
        return KitExecution(result.returncode, result.stdout)


def helper_main(argv: list[str]) -> int:
    """Run one bundled kit tool without starting the application or opening its DB."""
    if len(argv) < 4 or argv[1] != "--vault-root" or "--" not in argv[2:]:
        return 2
    tool, raw_root = argv[0], argv[2]
    marker = argv.index("--", 3)
    if marker != 3 or tool not in {"kb", "session", "display"}:
        return 2
    try:
        root = canonical_vault(raw_root)
        kit_root = bundled_kit_root()
        manifest = verify_source_kit(kit_root)
        if manifest.protocol_version != 2:
            return 2
        scripts = {
            "kb": "kb.py",
            "session": "wiki_session.py",
            "display": "wiki_display.py",
        }
        script = kit_root / "tools" / scripts[tool]
        arguments = argv[marker + 1:]
        old_argv = sys.argv
        old_path = list(sys.path)
        sys.argv = ([os.fspath(script), *arguments, "--root", os.fspath(root)]
                    if tool in {"kb", "display"}
                    else [os.fspath(script), "--root", os.fspath(root), *arguments])
        sys.path.insert(0, os.fspath(script.parent))
        try:
            runpy.run_path(os.fspath(script), run_name="__main__")
        except SystemExit as exit_status:
            return int(exit_status.code or 0) if isinstance(exit_status.code, int) else 2
        finally:
            sys.argv = old_argv
            sys.path[:] = old_path
        return 0
    except (OSError, WikiKitError, WikiKitRuntimeError, WikiLockError):
        return 2
