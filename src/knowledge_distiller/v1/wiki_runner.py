"""Tool-neutral wiki Agent boundary and the Codex CLI implementation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable

from .codex import _usage, executable, subscription_models
from .llm import LLMRequestError
from .wiki_kit_runtime import WikiKitRuntime, WikiKitRuntimeError
from .wiki_session_broker import WikiSessionBroker, WikiSessionBrokerError


class WikiRunnerError(RuntimeError):
    """A fixed-code runner error safe to cross the application boundary."""


@dataclass(frozen=True)
class RunnerResult:
    error_code: str | None
    usage: tuple[tuple[str, int], ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.error_code is None


_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "daemon_auto_start",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "workspace_dependencies",
)


def _safe_relative(value: str) -> str:
    from pathlib import PurePosixPath
    path = PurePosixPath(value)
    if (path.is_absolute() or path.suffix != ".md" or not path.parts
            or path.parts[0] != "raw" or any(part in {"", ".", ".."} for part in path.parts)):
        raise WikiRunnerError("batch_boundary_invalid")
    return path.as_posix()


def batch_prompt(batch_no: int, raw_paths: Iterable[str], *,
                 session_command: str = "python3 tools/wiki_session.py --root . status",
                 kb_command: str = "python3 tools/kb.py --root .") -> str:
    paths = tuple(_safe_relative(value) for value in raw_paths)
    if batch_no < 1 or not paths or len(set(paths)) != len(paths):
        raise WikiRunnerError("batch_boundary_invalid")
    listed = "\n".join(f"- `{path}`" for path in paths)
    return f"""你正在隔离 staging Vault 中执行 wiki 维护任务第 {batch_no} 批。
正式 Vault 不在你的工作区，也不得查找、访问或修改工作区之外的文件。

先完整读取根 AGENTS.md 和 `.agents/skills/kb-ingest/SKILL.md`，再运行
`{session_command}`；失败就停止。只处理以下冻结 raw，
每份完整读取，不处理其他 pending raw：
{listed}

严格按规则维护 wiki，raw 只读；不得修改工具包、.obsidian、附件、其他笔记，
不得创建工作区外文件。结束前运行 `{kb_command}`，修复全部错误。
本批不要执行 AI 体检；协调器会在 ingest 后用机器协议重新判断是否到期。
最终回复只简述完成状态；协调器以文件、机器检查和发布读回为准。
"""


def health_prompt(*, kb_command: str = "python3 tools/kb.py --root .") -> str:
    return f"""你正在隔离 staging Vault 中继续刚完成的最后一批 wiki 维护任务。
正式 Vault 不在你的工作区，也不得查找、访问或修改工作区之外的文件。
协调器已在 ingest 后用机器协议确认：待处理素材和待确认候选均为零，且完整体检到期。
完整读取根 AGENTS.md 的 7.6 和 `.agents/skills/kb-lint/SKILL.md`，只执行本轮完整体检。
不得修改 raw、工具包、.obsidian、附件、其他笔记或工作区外文件。结束前运行
`{kb_command}` 并修复全部错误。最终回复只简述完成状态；协调器以文件、
机器检查和发布读回为准。
"""


def _environment(session: WikiSessionBroker) -> dict[str, str]:
    allowed = {
        "CODEX_HOME", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "TMPDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(session.environment())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in tuple(environment):
        upper = key.upper()
        if key not in session.environment() and any(marker in upper for marker in (
                "API_KEY", "TOKEN", "SECRET", "CREDENTIAL")):
            environment.pop(key, None)
    return environment


def _toml_table(values: dict[str, str]) -> str:
    return "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "}"


def _command(codex: str, staging_vault: Path, model: str, effort: str,
             session_environment: dict[str, str]) -> list[str]:
    socket = json.dumps(session_environment["KD_WIKI_LOCK_SOCKET"])
    permissions = (
        '{description="Wiki staging",extends=":read-only",'
        'filesystem={":workspace_roots"={"."="write"}},'
        'network={enabled=true,mode="limited",allow_local_binding=false,'
        'domains={},unix_sockets={' + socket + '="allow"}}}'
    )
    command = [
        codex, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config",
        "--json",
        "--skip-git-repo-check", "-C", os.fspath(staging_vault),
        "--color", "never", "--model", model,
        "-c", 'model_provider="knowledge_subscription"',
        "-c", 'model_providers.knowledge_subscription.name="ChatGPT subscription"',
        "-c", "model_providers.knowledge_subscription.requires_openai_auth=true",
        "-c", "model_providers.knowledge_subscription.supports_websockets=false",
        "-c", 'forced_login_method="chatgpt"',
        "-c", 'approval_policy="never"',
        "-c", 'web_search="disabled"',
        "-c", "features.network_proxy=true",
        "-c", 'default_permissions="wiki_staging"',
        "-c", "permissions.wiki_staging=" + permissions,
        "-c", 'shell_environment_policy.inherit="core"',
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", "shell_environment_policy.experimental_use_profile=false",
        "-c", "shell_environment_policy.include_only=" + json.dumps([
            "HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "PYTHONDONTWRITEBYTECODE",
            "KD_WIKI_LOCK_VAULT_KEY", "KD_WIKI_LOCK_SOCKET", "KD_WIKI_LOCK_TOKEN",
        ]),
        "-c", "shell_environment_policy.set=" + _toml_table(session_environment),
        "-c", "features.shell_tool=true",
        "-c", "mcp_servers={}",
    ]
    for feature in _DISABLED_FEATURES:
        command.extend(("--disable", feature))
    if effort:
        command.extend(("-c", "model_reasoning_effort=" + json.dumps(effort)))
    command.append("-")
    return command


class CodexWikiRunner:
    def __init__(self, *, timeout_seconds: float = 900,
                 executable_resolver: Callable[[], str] = executable,
                 model_probe: Callable[[], list[dict]] = subscription_models,
                 kit_runtime: WikiKitRuntime | None = None):
        self.timeout_seconds = timeout_seconds
        self.executable_resolver = executable_resolver
        self.model_probe = model_probe
        self.kit_runtime = kit_runtime
        self._active_guard = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._cancel_requested = threading.Event()

    def cancel(self) -> None:
        self._cancel_requested.set()
        with self._active_guard:
            process = self._active_process
        if process is not None:
            self._terminate_group(process)

    def reset_cancellation(self) -> None:
        """Begin one worker lifetime; individual runs never erase cancellation."""
        with self._active_guard:
            if self._active_process is not None:
                raise WikiRunnerError("runner_unavailable")
            self._cancel_requested.clear()

    def preflight(self, model: str, effort: str) -> str:
        try:
            codex = self.executable_resolver()
            models = self.model_probe()
        except LLMRequestError as error:
            code = "config_required" if str(error) == "llm_config_unavailable" else "runner_unavailable"
            raise WikiRunnerError(code) from error
        except OSError as error:
            raise WikiRunnerError("runner_unavailable") from error
        row = next((item for item in models if item.get("model") == model), None)
        if row is None or effort not in row.get("efforts", ()):
            raise WikiRunnerError("model_unavailable")
        return codex

    def run(self, staging_vault: Path | str, runtime_root: Path | str, *, model: str,
            effort: str, batch_no: int, raw_paths: Iterable[str],
            skip_preflight: bool = False) -> RunnerResult:
        root = Path(staging_vault)
        commands = self._prompt_commands(root)
        prompt = batch_prompt(batch_no, raw_paths, **commands)
        return self._run_prompt(root, runtime_root, model=model, effort=effort, prompt=prompt,
                                skip_preflight=skip_preflight)

    def run_health(self, staging_vault: Path | str, runtime_root: Path | str, *, model: str,
                   effort: str, skip_preflight: bool = False) -> RunnerResult:
        root = Path(staging_vault)
        prompt = health_prompt(kb_command=self._prompt_commands(root)["kb_command"])
        return self._run_prompt(root, runtime_root, model=model, effort=effort,
                                prompt=prompt, skip_preflight=skip_preflight)

    def _prompt_commands(self, root: Path) -> dict[str, str]:
        if self.kit_runtime is None:
            return {
                "session_command": "python3 tools/wiki_session.py --root . status",
                "kb_command": "python3 tools/kb.py --root .",
            }
        try:
            return {
                "session_command": self.kit_runtime.shell_command("session", root, ("status",)),
                "kb_command": self.kit_runtime.shell_command("kb", root),
            }
        except WikiKitRuntimeError as error:
            raise WikiRunnerError(str(error)) from error

    def _run_prompt(self, root: Path, runtime_root: Path | str, *, model: str,
                    effort: str, prompt: str, skip_preflight: bool) -> RunnerResult:
        try:
            codex = self.executable_resolver() if skip_preflight else self.preflight(model, effort)
            with WikiSessionBroker(root, runtime_root) as session:
                session_environment = session.environment()
                with self._active_guard:
                    if self._cancel_requested.is_set():
                        return RunnerResult("interrupted")
                    process = subprocess.Popen(
                        _command(codex, root, model, effort, session_environment),
                        cwd=root,
                        env=_environment(session),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        start_new_session=(os.name == "posix"),
                        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                                       if sys.platform == "win32" else 0),
                    )
                    self._active_process = process
                try:
                    try:
                        stdout, _stderr = process.communicate(prompt, timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        self._terminate_group(process)
                        return RunnerResult("interrupted" if self._cancel_requested.is_set()
                                            else "runner_timeout")
                    except UnicodeError:
                        self._terminate_group(process)
                        return RunnerResult("agent_failed")
                    if self._cancel_requested.is_set():
                        return RunnerResult("interrupted")
                    usage = tuple(sorted(_usage(stdout).items()))
                    return RunnerResult(None if process.returncode == 0 else "agent_failed", usage)
                finally:
                    with self._active_guard:
                        if self._active_process is process:
                            self._active_process = None
        except WikiRunnerError:
            raise
        except WikiSessionBrokerError as error:
            code = str(error)
            raise WikiRunnerError(code if code == "vault_busy" else "runner_unavailable") from error
        except OSError as error:
            raise WikiRunnerError("runner_unavailable") from error

    @staticmethod
    def _terminate_group(process: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # macOS can report EPERM for a group containing only a
                    # reparented zombie. Still attempt SIGKILL below.
                    break
                time.sleep(0.05)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
