"""Verify a newly built candidate with disposable data, never the installed app."""
import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import runpy
import sqlite3
import subprocess
import time
import urllib.request


PYTHON_VERSION = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "src/knowledge_distiller/v1/adapters/python_policy.py")
)["PYTHON_VERSION"]
HASH = re.compile(r"[0-9a-f]{64}\Z")
RAW_ID = re.compile(r"R-\d{8}-\d{4}\Z")
PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_HELPER_OUTPUT = 1_000_000


class CandidateVerificationError(RuntimeError):
    """A fixed-code candidate failure that is safe to expose."""


def _fail(code: str) -> None:
    raise CandidateVerificationError(code)


def _fixed_error(error: BaseException) -> str:
    if isinstance(error, CandidateVerificationError) and re.fullmatch(
        r"[a-z][a-z0-9_]*", str(error)
    ):
        return str(error)
    return "candidate_verification_failed"


def _integer(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _validate_protocol_scan(payload: object) -> dict[str, object]:
    """Validate the complete frozen protocol without retaining Vault content."""
    if not isinstance(payload, dict) or set(payload) != {
        "protocol_version", "issue_counts", "pending", "candidate_count", "health"
    }:
        _fail("wiki_protocol_result_invalid")
    counts = payload["issue_counts"]
    if (payload["protocol_version"] != 2
            or not isinstance(counts, dict)
            or set(counts) != {"错误", "提醒", "信息"}
            or any(not _integer(value) for value in counts.values())
            or counts["错误"] != 0
            or not _integer(payload["candidate_count"])):
        _fail("wiki_protocol_result_invalid")
    health = payload["health"]
    if (not isinstance(health, dict)
            or set(health) != {
                "eligible", "due", "due_reason", "last_lint_date", "lint_count"
            }
            or type(health["eligible"]) is not bool
            or type(health["due"]) is not bool
            or health["due_reason"] not in {
                "not_eligible", "first", "current", "changed_due",
                "changed_waiting", "unknown",
            }
            or not isinstance(health["last_lint_date"], str)
            or not _integer(health["lint_count"])
            or (health["due"] and not health["eligible"])):
        _fail("wiki_protocol_result_invalid")
    pending = payload["pending"]
    expected = {
        "relative_path", "raw_id", "identity", "collected_at", "addendum_target",
        "adjacent_raw_ids", "byte_count", "content_sha256",
    }
    if not isinstance(pending, list) or len(pending) != 1:
        _fail("wiki_protocol_result_invalid")
    for row in pending:
        if not isinstance(row, dict) or set(row) != expected:
            _fail("wiki_protocol_result_invalid")
        relative = row["relative_path"]
        raw_id = row["raw_id"]
        adjacent = row["adjacent_raw_ids"]
        try:
            parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
        except ValueError:
            parts = ()
        if (not isinstance(relative, str) or len(parts) < 3
                or parts[0] != "raw" or parts[1] not in {"外部", "自述"}
                or any(part in {"", ".", ".."} for part in parts)
                or not isinstance(raw_id, str) or RAW_ID.fullmatch(raw_id) is None
                or PurePosixPath(relative).stem != raw_id
                or row["identity"] not in {"第三方", "本人", "本人附言"}
                or (row["identity"] == "第三方") != (parts[1] == "外部")
                or not isinstance(row["collected_at"], str) or not row["collected_at"]
                or not isinstance(row["addendum_target"], str)
                or (row["addendum_target"] and RAW_ID.fullmatch(row["addendum_target"]) is None)
                or not isinstance(adjacent, list)
                or any(not isinstance(value, str) or RAW_ID.fullmatch(value) is None
                       for value in adjacent)
                or not _integer(row["byte_count"])
                or not isinstance(row["content_sha256"], str)
                or HASH.fullmatch(row["content_sha256"]) is None):
            _fail("wiki_protocol_result_invalid")
    return {"protocol_version": 2, "structure_valid": True}


def _validate_display_plan(payload: object) -> dict[str, object]:
    if (not isinstance(payload, dict)
            or set(payload) != {"state", "plan_id", "page_count", "raw_sha256"}
            or payload["state"] != "planned"
            or not isinstance(payload["plan_id"], str)
            or PLAN_ID.fullmatch(payload["plan_id"]) is None
            or not _integer(payload["page_count"], minimum=1)
            or not isinstance(payload["raw_sha256"], str)
            or HASH.fullmatch(payload["raw_sha256"]) is None):
        _fail("wiki_display_result_invalid")
    return {
        "state": "planned",
        "page_count": payload["page_count"],
        "structure_valid": True,
    }


def _helper_json(command: list[str], *, env: dict[str, str], cwd: Path,
                 failure_code: str) -> object:
    try:
        result = subprocess.run(
            command, env=env, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", check=False, timeout=120,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        _fail(failure_code)
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > MAX_HELPER_OUTPUT:
        _fail(failure_code)
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError):
        _fail(failure_code)


def _synthetic_vault(output: Path) -> Path:
    vault = output / "synthetic-vault"
    raw = vault / "raw/外部/2026/10/R-20261001-0001.md"
    page = vault / "wiki/index.md"
    skills = vault / "skills"
    raw.parent.mkdir(parents=True)
    page.parent.mkdir(parents=True)
    skills.mkdir(parents=True)
    raw.write_text(
        "---\n编号: R-20261001-0001\n格式版本: 1\n身份: 第三方\n"
        "收录于: 2026-10-01T10:00:00+08:00\n---\n\n"
        "候选验证使用的合成素材。\n\n^source-1\n",
        encoding="utf-8",
    )
    page.write_text(
        "# 合成知识库\n\n仅用于检查冻结工具的结构化协议。\n",
        encoding="utf-8",
    )
    return vault


def _vault_bytes(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*")) if path.is_file()
    }


def _isolate_mac_home(environment: dict[str, str], output: Path) -> None:
    home = output / "candidate-home"
    home.mkdir(mode=0o700)
    for key in tuple(environment):
        if key == "CODEX_HOME" or key.startswith("CODEX_") or key in {
            "OPENAI_API_KEY", "OPENAI_API_TOKEN",
        }:
            environment.pop(key)
    environment["HOME"] = str(home)


def _verify_frozen_wiki_helpers(executable: Path, output: Path,
                                environment: dict[str, str]) -> tuple[Path, dict[str, object]]:
    vault = _synthetic_vault(output)
    before = _vault_bytes(vault)
    helper_home = output / "wiki-helper-home"
    helper_home.mkdir(mode=0o700)
    helper_env = {
        key: environment[key] for key in ("PATH", "LANG", "LC_ALL")
        if key in environment
    }
    helper_env.setdefault("LANG", "C.UTF-8")
    helper_env.setdefault("LC_ALL", "C.UTF-8")
    helper_env["HOME"] = str(helper_home)
    protocol = _helper_json(
        [str(executable), "--wiki-kit", "kb", "--vault-root", str(vault),
         "--", "protocol-scan"],
        env=helper_env, cwd=output, failure_code="wiki_protocol_helper_failed",
    )
    protocol_summary = _validate_protocol_scan(protocol)
    display = _helper_json(
        [str(executable), "--wiki-kit", "display", "--vault-root", str(vault),
         "--", "plan", "--journal-root", str(output / "wiki-display-journal")],
        env=helper_env, cwd=output, failure_code="wiki_display_helper_failed",
    )
    display_summary = _validate_display_plan(display)
    if _vault_bytes(vault) != before:
        _fail("wiki_helper_changed_vault")
    forbidden = {"knowledge.sqlite3", ".desktop-instance.json"}
    app_database_started = any(
        path.name in forbidden for path in output.rglob("*")
    )
    if app_database_started:
        _fail("wiki_helper_started_app")
    return vault, {
        "synthetic_vault": True,
        "protocol_scan": protocol_summary,
        "display_plan": display_summary,
        "app_database_started": False,
    }


class _WikiWorkflowEntryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.target_depth = 0
        self.section_found = False
        self.form_found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "section" and values.get("data-sync-key") == "organization":
            self.target_depth = 1
            self.section_found = True
            return
        if self.target_depth and tag == "section":
            self.target_depth += 1
        if (self.target_depth and tag == "form"
                and values.get("id") == "wiki-submit"
                and values.get("method", "").lower() == "post"
                and values.get("action") == "/organization"):
            self.form_found = True

    def handle_endtag(self, tag: str) -> None:
        if self.target_depth and tag == "section":
            self.target_depth -= 1


def _verify_wiki_workflow_entry(page: bytes) -> bool:
    if len(page) > MAX_HELPER_OUTPUT:
        _fail("wiki_workflow_entry_missing")
    try:
        parser = _WikiWorkflowEntryParser()
        parser.feed(page.decode("utf-8"))
    except (UnicodeError, ValueError):
        _fail("wiki_workflow_entry_missing")
    if not parser.section_found or not parser.form_found:
        _fail("wiki_workflow_entry_missing")
    return True


def _seed_synthetic_wiki_settings(database: Path, vault: Path) -> None:
    """Configure only the candidate-owned disposable database between launches."""
    try:
        with sqlite3.connect(database) as connection:
            connection.executemany(
                """INSERT INTO settings(key,value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("vault_path", str(vault)),
                    ("llm_provider", "codex"),
                    ("llm_model", "synthetic-candidate-model"),
                    ("llm_effort", "medium"),
                    ("llm_state", "configured"),
                ),
            )
    except (OSError, sqlite3.Error):
        _fail("wiki_synthetic_settings_failed")


def _fetch_page(port: int, route: str) -> bytes:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}" + route, timeout=10
        ) as response:
            if response.status != 200:
                _fail("candidate_page_failed")
            return response.read(MAX_HELPER_OUTPUT + 1)
    except CandidateVerificationError:
        raise
    except (OSError, ValueError):
        _fail("candidate_page_failed")


def _wait_for_wiki_entry(port: int, *, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        page = _fetch_page(port, "/")
        try:
            return _verify_wiki_workflow_entry(page)
        except CandidateVerificationError as error:
            if str(error) != "wiki_workflow_entry_missing" or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _wait_for_candidate_ready(process: subprocess.Popen, state: Path,
                              *, timeout: float = 40,
                              require_process_pid: bool = True) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while not state.exists():
        if process.poll() is not None:
            _fail("candidate_exited_before_ready")
        if time.monotonic() > deadline:
            _fail("candidate_startup_timeout")
        time.sleep(0.1)
    try:
        record = json.loads(state.read_text(encoding="utf-8"))
        port = record["port"]
        pid = record["pid"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        _fail("candidate_state_invalid")
    if (type(port) is not int or not 1 <= port <= 65535
            or type(pid) is not int or pid <= 0
            or (require_process_pid and pid != process.pid)):
        _fail("candidate_state_invalid")
    return record


def _wait_for_candidate_exit(process: subprocess.Popen, state: Path,
                             *, timeout: float = 35) -> None:
    try:
        process.terminate()
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _fail("candidate_exit_timeout")
    except OSError:
        _fail("candidate_exit_failed")
    if return_code != 0:
        _fail("candidate_exit_failed")
    if state.exists():
        _fail("candidate_state_not_removed")


def _cleanup_candidate(process: subprocess.Popen, state: Path,
                       *, timeout: float = 15) -> bool:
    """Stop only this child; remove its owned stale state after a forced stop."""
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                return False
        except OSError:
            return False
    if state.exists():
        try:
            record = json.loads(state.read_text(encoding="utf-8"))
            owned = record.get("pid") == process.pid
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            owned = False
        if owned:
            try:
                state.unlink()
            except OSError:
                return False
    return process.poll() is not None


def _finish_candidate_cleanup(stage_error: BaseException | None, cleaned: bool,
                              report: dict[str, object]) -> None:
    if not cleaned:
        report["cleanup_error"] = "candidate_cleanup_failed"
    if stage_error is not None:
        raise stage_error
    if not cleaned:
        _fail("candidate_cleanup_failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["mac", "windows"], required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    build = args.build.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "ok": False,
        "platform": args.platform,
        "version": args.version,
        "source_commit": args.commit,
        "disposable_data": True,
    }
    failure = None
    try:
        manifest = json.loads((build / "build-manifest.json").read_text(encoding="utf-8"))
        assert manifest["python"] == PYTHON_VERSION and manifest["python_inventory"]
        assert manifest["git_head"] == args.commit and manifest["version"] == args.version
        assert manifest["status"] in {"built", "built-not-yet-accepted"}
        assert not manifest.get("changed_during_build") and not manifest.get("git_dirty")
        if args.platform == "mac":
            app = build / "知识蒸馏器.app"
            executable = app / "Contents/MacOS/KnowledgeDistiller"
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            assert info["CFBundleVersion"] == args.version
            assert info.get("KDManualUpdateOnly") is False, "正式候选必须支持差量安装"
            assert info.get("SURequireSignedFeed") and info.get("SUPublicEDKey") and info.get("SUFeedURL")
            assert info.get("KDCodeSigningMode") == "local-certificate"
            assert not info.get("KDUpdateTestDataRoot")
            signature = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(app)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if signature.returncode != 0:
                _fail("candidate_signature_invalid")
            report["signature"] = "deep strict; local-certificate"
        else:
            app = build / "KnowledgeDistiller"
            executable = app / "KnowledgeDistiller.exe"
            info = json.loads((app / "_internal/windows-version.json").read_text(encoding="utf-8"))
            assert info["version"] == args.version and info["source_commit"] == args.commit
            assert info.get("feed_url") and info.get("public_key"), "Windows正式候选必须配置签名更新源"
            assert (app / "update-helper.exe").is_file(), "Windows正式候选缺少更新安装器"
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "CONDA", "VIRTUAL_ENV", "KNOWLEDGE_DISTILLER"))
        }
        env.update(
            HF_HOME=str(output / "empty-hf"),
            HF_HUB_OFFLINE="1",
            PADDLE_PDX_CACHE_HOME=str(output / "empty-paddle"),
        )
        if args.platform == "windows":
            env.update(
                PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
                LOCALAPPDATA=str(output / "LocalAppData"),
            )
        else:
            env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            _isolate_mac_home(env, output)
            vault, wiki_helpers = _verify_frozen_wiki_helpers(executable, output, env)
            report["wiki_helpers"] = wiki_helpers
        helper = app / (
            "Contents/MacOS/update-helper" if args.platform == "mac" else "update-helper.exe"
        )
        helper_result = subprocess.run(
            [str(helper), "--runtime-report", str(output / "helper-runtime.json")],
            env=env, cwd=output, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=60,
        )
        if helper_result.returncode != 0:
            _fail("update_helper_runtime_failed")
        helper_runtime = json.loads((output / "helper-runtime.json").read_text(encoding="utf-8"))
        assert helper_runtime["frozen"] and helper_runtime["python"]["version"] == PYTHON_VERSION
        report["update_helper_runtime"] = helper_runtime
        base = [str(executable), "--data-dir", str(output / "data"), "--no-open"]
        with (output / "runtime.log").open("wb") as log:
            command = base + ["--check-runtime", str(output / "runtime.json")]
            if args.platform == "windows":
                command += ["--check-offline"]
            subprocess.run(
                command, env=env, cwd=output, stdout=log, stderr=log,
                check=True, timeout=180,
            )
        runtime = json.loads((output / "runtime.json").read_text(encoding="utf-8"))
        assert runtime["python"]["version"] == PYTHON_VERSION and runtime["python_inventory"]
        assert runtime["ok"] and runtime["frozen"]
        report["runtime"] = runtime
        ports = []
        wiki_workflow_entry = False
        for iteration in range(2):
            with (output / "launch.log").open("ab") as log:
                command = base + (["--smoke-seconds", "12"] if args.platform == "windows" else [])
                try:
                    process = subprocess.Popen(
                        command, env=env, cwd=output, stdout=log, stderr=log,
                    )
                except OSError:
                    _fail("candidate_launch_failed")
                stage_error = None
                try:
                    state = output / "data/.desktop-instance.json"
                    record = _wait_for_candidate_ready(
                        process, state, require_process_pid=args.platform == "mac"
                    )
                    ports.append(record["port"])
                    for route in ("/", "/topics", "/insights", "/settings"):
                        _fetch_page(record["port"], route)
                    if args.platform == "mac" and iteration == 1:
                        wiki_workflow_entry = _wait_for_wiki_entry(record["port"])
                    if args.platform == "mac":
                        _wait_for_candidate_exit(process, state)
                    else:
                        try:
                            return_code = process.wait(timeout=35)
                        except subprocess.TimeoutExpired:
                            _fail("candidate_exit_timeout")
                        if return_code != 0:
                            _fail("candidate_exit_failed")
                        if state.exists():
                            _fail("candidate_state_not_removed")
                except BaseException as error:
                    stage_error = error
                finally:
                    cleaned = _cleanup_candidate(process, state)
                _finish_candidate_cleanup(stage_error, cleaned, report)
            if args.platform == "mac" and iteration == 0:
                _seed_synthetic_wiki_settings(output / "data/knowledge.sqlite3", vault)
        if ports != [57740, 57740]:
            _fail("candidate_fixed_port_invalid")
        if args.platform == "mac":
            if not wiki_workflow_entry:
                _fail("wiki_workflow_entry_missing")
            report["wiki_workflow_entry"] = True
        (output / "support.md").write_text(
            "候选构建验收：版本、源码提交、冻结运行依赖、四个页面、固定端口及两次启动退出已通过。\n"
            + ("已用显式合成知识库核对冻结工具结构协议与知识整理入口；不表示真实 Codex 任务或正式数据升级已经验收。\n"
               if args.platform == "mac" else "")
            + "本报告不表示公开发布或正式数据升级已批准。Windows桌面凭据另以合成数据验收；不包含Windows 11或ARM64支持承诺。\n",
            encoding="utf-8",
        )
        report.update(ok=True, fixed_port=ports[0], launches=2, pages=4)
    except BaseException as error:
        failure = _fixed_error(error)
        report["error"] = failure
    finally:
        (output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if failure is not None:
        raise SystemExit(failure)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit("candidate_verification_failed") from None
