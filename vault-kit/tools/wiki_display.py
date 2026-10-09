#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Knowledge-wiki display metadata and an explicit, recoverable migration.

The migration changes presentation only.  ``plan`` reads the Vault and writes a
private plan outside it.  ``apply`` and ``revert`` require the caller to be in a
live ``wiki_session`` and use exact-byte compare-before-replace checks.  A run is
recoverable one file at a time; it is deliberately not described as an atomic
multi-file transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DISPLAY_VERSION = 1
PROGRAM_FIELDS = {"编号", "类型", "创建", "更新"}
AUTO_SUFFIX = "（自动）"
START_RE = re.compile(
    r"^<!-- kd-wiki-display:v1 inner=([0-9a-f]{64}) "
    r"added=([a-z0-9,_-]+|-) -->$"
)
END_MARK = "<!-- /kd-wiki-display -->"


@dataclass(frozen=True)
class DisplaySpec:
    page_type: str
    css_class: str
    label: str
    role: str
    detail: str


FOLDER_SPECS = {
    "来源": ("来源", "kd-wiki-source", "来源", "第三方观点"),
    "概念": ("概念", "kd-wiki-concept", "概念", "他人观点与术语"),
    "认知": ("认知", "kd-wiki-cognition", "认知", "用户怎么看"),
    "方法": ("方法", "kd-wiki-method", "方法", "一般怎么做"),
    "技能": ("技能", "kd-wiki-skill", "技能", "具体怎么做"),
    "实践": ("实践", "kd-wiki-practice", "实践", "发生了什么"),
    "综合": ("综合", "kd-wiki-synthesis", "综合", "已获同意的派生回答"),
    "主题": ("主题", "kd-wiki-topic", "主题", "导览与汇总"),
}
SYSTEM_SPECS = {
    "wiki/index.md": ("index", "kd-wiki-index", "日常入口", "脚本生成", "更新由脚本维护"),
    "wiki/待确认.md": ("pending", "kd-wiki-pending", "待确认", "脚本生成", "回复“对 / 不对 / 改成……”"),
    "wiki/log.md": ("log", "kd-wiki-log", "日志", "只追加", "保留知识活动记录"),
    "wiki/体检报告.md": ("health", "kd-wiki-health", "体检", "AI 检查记录", "用于人工复核"),
}


class DisplayError(RuntimeError):
    """Fixed-code error that is safe to expose from the CLI."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    if text.startswith("---"):
        lines = text.split("\n")
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[1:index], "\n".join(lines[index + 1:])
    return None, text


def _render_frontmatter(lines: list[str] | None, body: str) -> str:
    if lines is None:
        return body
    return "\n".join(["---", *lines, "---", body])


def _parse_value(value: str):
    value = re.sub(r"\s+#.*$", "", value).strip()
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return value.strip("'\"")


def _metadata(lines: list[str] | None) -> dict[str, object]:
    result: dict[str, object] = {}
    for line in lines or []:
        match = re.match(r"^([^\s:#][^:]*):\s*(.*)$", line)
        if match:
            result[match.group(1).strip()] = _parse_value(match.group(2))
    return result


def _page_spec(relative_path: str, metadata: dict[str, object] | None = None) -> DisplaySpec:
    if relative_path in SYSTEM_SPECS:
        page_type, css_class, label, role, detail = SYSTEM_SPECS[relative_path]
        return DisplaySpec(page_type, css_class, label, role, detail)
    parts = Path(relative_path).parts
    if len(parts) != 3 or parts[0] != "wiki" or Path(parts[2]).suffix != ".md":
        raise DisplayError("display_path_unsupported")
    values = FOLDER_SPECS.get(parts[1])
    if values is None:
        raise DisplayError("display_path_unsupported")
    page_type, css_class, label, role = values
    meta = metadata or {}
    candidate = meta.get("确认") == "候选"
    if candidate and page_type in {"认知", "方法", "技能", "实践"}:
        label += "候选"
    if page_type == "来源":
        detail = "原始材料可追溯"
        if meta.get("发布日期"):
            detail += f" · {meta['发布日期']}"
    elif page_type == "概念":
        detail = "不代表用户已经认可"
    elif page_type in {"认知", "方法"}:
        detail = str(meta.get("证据") or "证据状态未填写")
        if candidate:
            detail += " · 等待用户确认"
    elif page_type == "技能":
        detail = "薄入口" + (" · 等待用户确认" if candidate else "")
    elif page_type == "实践":
        detail = str(meta.get("结果") or "结果未填写")
        if candidate:
            detail += " · 等待用户确认"
    elif page_type == "综合":
        detail = "不能作为其他页面的依据"
    else:
        detail = "概览由 AI 维护 · 其余小节由脚本生成"
    return DisplaySpec(page_type, css_class, label, role, detail)


def _css_region(lines: list[str]) -> tuple[int, int, list[str]] | None:
    """Return the exact cssclasses YAML region and its semantic tokens."""
    for start, line in enumerate(lines):
        match = re.match(r"^(\s*)cssclasses\s*:\s*(.*)$", line)
        if not match:
            continue
        value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
        if value.startswith("[") and value.endswith("]"):
            tokens = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
            return start, start + 1, tokens
        if value:
            # Changing scalar YAML would necessarily rewrite its format.  Stop
            # conservatively rather than taking ownership of user formatting.
            raise DisplayError("cssclasses_scalar_unsupported")
        end = start + 1
        tokens: list[str] = []
        while end < len(lines):
            item = re.match(r"^\s+-\s+(.+?)\s*$", lines[end])
            if not item:
                break
            tokens.append(re.sub(r"\s+#.*$", "", item.group(1)).strip().strip("'\""))
            end += 1
        return start, end, tokens
    return None


def _css_exact(lines: list[str]) -> tuple[bytes, list[str]]:
    region = _css_region(lines)
    if region is None:
        return b"", []
    start, end, tokens = region
    return "\n".join(lines[start:end]).encode("utf-8"), tokens


def _add_cssclasses(lines: list[str], wanted: Iterable[str]) -> tuple[list[str], list[str]]:
    result = list(lines)
    region = _css_region(result)
    wanted_list = list(wanted)
    if region is None:
        added = wanted_list
        result.append("cssclasses: [" + ", ".join(added) + "]")
    else:
        start, end, tokens = region
        added = [token for token in wanted_list if token not in tokens]
        if added:
            first = result[start]
            value = re.sub(r"\s+#.*$", "", first.split(":", 1)[1]).strip()
            if value.startswith("[") and value.endswith("]"):
                close = first.rfind("]")
                prefix = first[:close]
                separator = "" if prefix.rstrip().endswith("[") else ", "
                result[start] = prefix + separator + ", ".join(added) + first[close:]
            else:
                indent = "  "
                if end > start + 1:
                    indent = re.match(r"^(\s*)", result[start + 1]).group(1)
                result[end:end] = [f"{indent}- {token}" for token in added]
    return result, added


def _canonical_css(lines: list[str], remove: Iterable[str] = ()) -> list[str]:
    result = list(lines)
    region = _css_region(result)
    if region is None:
        return result
    start, end, tokens = region
    remaining = list(tokens)
    for token in remove:
        if token not in remaining:
            raise DisplayError("display_class_proof_invalid")
        remaining.remove(token)
    replacement = ["cssclasses: [" + ", ".join(remaining) + "]"] if remaining else []
    result[start:end] = replacement
    return result


def _display_parts(body: str):
    lines = body.split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith("<!-- kd-wiki-display:")]
    ends = [index for index, line in enumerate(lines) if line == END_MARK]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise DisplayError("display_marker_invalid")
    start, end = starts[0], ends[0]
    marker = START_RE.fullmatch(lines[start])
    if marker is None:
        raise DisplayError("display_marker_invalid")
    inner = "\n".join(lines[start + 1:end])
    if sha256(inner.encode("utf-8")) != marker.group(1):
        raise DisplayError("display_block_modified")
    added = [] if marker.group(2) == "-" else marker.group(2).split(",")
    return lines, start, end, added, inner


def _wanted_classes(relative_path: str, spec: DisplaySpec) -> list[str]:
    wanted = ["kd-reading", "kd-wiki"]
    if relative_path in SYSTEM_SPECS:
        wanted.append("kd-wiki-system")
    wanted.append(spec.css_class)
    return wanted


def _validate_display_proof(inner: str, added: list[str], relative_path: str,
                            spec: DisplaySpec) -> None:
    allowed = _wanted_classes(relative_path, spec)
    if len(added) != len(set(added)) or any(token not in allowed for token in added):
        raise DisplayError("display_class_proof_invalid")
    lines = inner.split("\n")
    title = Path(relative_path).stem
    if lines[:2] == [f"# {title}", ""]:
        lines = lines[2:]
    elif lines and lines[0].startswith("# "):
        raise DisplayError("display_block_invalid")
    if len(lines) != 3 or lines[0] != "> [!kd-page]":
        raise DisplayError("display_block_invalid")
    heading = re.fullmatch(r"> \*\*([^*]+)\*\* · (.+)  ", lines[1])
    allowed_labels = {spec.label}
    if spec.page_type in {"认知", "方法", "技能", "实践"}:
        base = spec.label.removesuffix("候选")
        allowed_labels |= {base, base + "候选"}
    if heading is None or heading.group(1) not in allowed_labels or heading.group(2) != spec.role:
        raise DisplayError("display_block_invalid")
    detail = lines[2]
    if (not detail.startswith("> ") or not (1 <= len(detail[2:]) <= 160)
            or "[[" in detail or "raw/" in detail or "<!--" in detail):
        raise DisplayError("display_block_invalid")


def _callout(spec: DisplaySpec, title: str, *, include_title: bool) -> str:
    parts = []
    if include_title:
        parts += [f"# {title}", ""]
    parts += [
        "> [!kd-page]",
        f"> **{spec.label}** · {spec.role}  ",
        f"> {spec.detail}",
    ]
    return "\n".join(parts)


def add_or_refresh_display(text: str, relative_path: str, *, detail: str | None = None) -> tuple[str, bool]:
    """Return the desired display form without overwriting a modified block."""
    fm, body = _split_frontmatter(text)
    lines = list(fm or [])
    metadata = _metadata(lines)
    spec = _page_spec(relative_path, metadata)
    if detail is not None:
        spec = DisplaySpec(spec.page_type, spec.css_class, spec.label, spec.role, detail)
    existing = _display_parts(body)
    if existing is not None:
        body_lines, start, end, added, existing_inner = existing
        _validate_display_proof(existing_inner, added, relative_path, spec)
        _, tokens = _css_exact(lines)
        # The marker records exactly which token occurrences the product added.
        # User classes may be added or reordered later; those remain business
        # metadata and must not invalidate or be stripped with our occurrences.
        if any(token not in tokens for token in added):
            raise DisplayError("display_class_proof_invalid")
        wanted = _wanted_classes(relative_path, spec)
        if any(token not in tokens for token in wanted):
            raise DisplayError("display_classes_modified")
        included_title = any(line.startswith("# ") for line in body_lines[start + 1:end])
        desired_inner = _callout(spec, Path(relative_path).stem, include_title=included_title)
        desired_marker = (
            f"<!-- kd-wiki-display:v1 inner={sha256(desired_inner.encode('utf-8'))} "
            f"added={','.join(added) if added else '-'} -->"
        )
        replacement = [desired_marker, *desired_inner.split("\n"), END_MARK]
        if body_lines[start:end + 1] == replacement:
            return text, False
        body_lines[start:end + 1] = replacement
        return _render_frontmatter(lines, "\n".join(body_lines)), True

    # An unmarked callout may be a user's own content or an earlier specimen.
    # Its origin cannot be proven from identical Markdown bytes, so never add a
    # second visible header and never silently take ownership of the first one.
    if re.search(r"^> \[!kd-page\]\s*$", body, re.M):
        raise DisplayError("display_callout_unowned")

    wanted = _wanted_classes(relative_path, spec)
    lines, added = _add_cssclasses(lines, wanted)
    body_lines = body.split("\n")
    h1_indexes = [index for index, line in enumerate(body_lines) if line.startswith("# ")]
    include_title = not h1_indexes
    inner = _callout(spec, Path(relative_path).stem, include_title=include_title)
    marker = (
        f"<!-- kd-wiki-display:v1 inner={sha256(inner.encode('utf-8'))} "
        f"added={','.join(added) if added else '-'} -->"
    )
    block = [marker, *inner.split("\n"), END_MARK]
    insert_at = h1_indexes[0] + 1 if h1_indexes else 0
    if insert_at < len(body_lines) and body_lines[insert_at] == "":
        insert_at += 1
    body_lines[insert_at:insert_at] = block + [""]
    return _render_frontmatter(lines, "\n".join(body_lines)), True


def _strip_verified_display(text: str, relative_path: str) -> str:
    fm, body = _split_frontmatter(text)
    lines = list(fm or [])
    parts = _display_parts(body)
    if parts is None:
        return _render_frontmatter(_canonical_css(lines), body)
    body_lines, start, end, added, inner = parts
    spec = _page_spec(relative_path, _metadata(lines))
    _validate_display_proof(inner, added, relative_path, spec)
    lines = _canonical_css(lines, added)
    del body_lines[start:end + 1]
    if start < len(body_lines) and body_lines[start] == "" and start and body_lines[start - 1] == "":
        del body_lines[start]
    return _render_frontmatter(lines, "\n".join(body_lines))


def _hash_material(text: str) -> bytes:
    fm, body = _split_frontmatter(text)
    kept = [line for line in (fm or [])
            if line.split(":", 1)[0].strip() not in PROGRAM_FIELDS]
    parts = ["\n".join(kept)]
    sections: list[tuple[str | None, list[str]]] = []
    current_head: str | None = None
    current: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            sections.append((current_head, current))
            current_head, current = line[3:].strip(), []
        else:
            current.append(line)
    sections.append((current_head, current))
    for current_head, current in sections:
        if current_head and current_head.endswith(AUTO_SUFFIX):
            parts.append(current_head)
        else:
            parts.append((current_head or "") + "\n" + "\n".join(current))
    return "\n".join(parts).encode("utf-8")


def render_system_document(relative_path: str, title: str, body: str, *, detail: str) -> str:
    """Render one of the four approved system-page shells."""
    base = f"# {title}\n\n{body.lstrip()}"
    rendered, _ = add_or_refresh_display(base, relative_path, detail=detail)
    return rendered


def legacy_content_hash(text: str) -> str:
    """The phase-3 SHA-1 algorithm, retained for a proven per-page handoff."""
    return hashlib.sha1(_hash_material(text)).hexdigest()


def business_content_hash(text: str, relative_path: str) -> str:
    return hashlib.sha1(_hash_material(_strip_verified_display(text, relative_path))).hexdigest()


def display_free_text(text: str, relative_path: str) -> str:
    """Remove only a fully verified product display block and its owned tokens."""
    return _strip_verified_display(text, relative_path)


def business_sha256(text: str, relative_path: str) -> str:
    return sha256(_hash_material(_strip_verified_display(text, relative_path)))


def identity_sha256(text: str, relative_path: str) -> str:
    fm, _ = _split_frontmatter(_strip_verified_display(text, relative_path))
    meta = _metadata(fm)
    value = [relative_path, Path(relative_path).stem, meta.get("编号", ""), meta.get("类型", "")]
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def references_sha256(text: str, relative_path: str) -> str:
    clean = _strip_verified_display(text, relative_path)
    links = re.findall(r"\[\[([^\]]+)\]\]", clean)
    raws = re.findall(r"raw/[^\s)）\]】，,；;。|]+", clean)
    return sha256(json.dumps({"links": links, "raw": raws}, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8"))


def _regular_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise DisplayError("path_symlink")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise DisplayError("path_missing") from None
    if not stat.S_ISREG(mode):
        raise DisplayError("path_not_regular")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DisplayError("path_not_regular")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _validate_tree_path(root: Path, path: Path) -> None:
    root = root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise DisplayError("path_outside_vault") from None
    current = root
    for part in relative.parts:
        current = current / part
        # is_symlink() uses lstat and therefore also catches a dangling link.
        if current.is_symlink():
            raise DisplayError("path_symlink")


def _raw_manifest(root: Path) -> list[dict[str, str]]:
    raw_root = root / "raw"
    if not raw_root.exists():
        return []
    if raw_root.is_symlink():
        raise DisplayError("raw_path_invalid")
    result = []
    for path in sorted(raw_root.rglob("*")):
        if path.is_dir():
            if path.is_symlink():
                raise DisplayError("raw_path_invalid")
            continue
        _validate_tree_path(root, path)
        data = _regular_bytes(path)
        result.append({"path": path.relative_to(root).as_posix(), "sha256": sha256(data)})
    return result


def _raw_digest(manifest: list[dict[str, str]]) -> str:
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _vault_pages(root: Path) -> list[Path]:
    pages = []
    for folder in FOLDER_SPECS:
        directory = root / "wiki" / folder
        if directory.is_dir():
            pages.extend(sorted(directory.glob("*.md")))
    for relative in SYSTEM_SPECS:
        path = root / relative
        if path.exists():
            pages.append(path)
    return pages


def _state_after(root: Path, entries: list[dict[str, object]]) -> tuple[bytes | None, bytes | None]:
    path = root / ".graph/state.json"
    if not path.exists():
        return None, None
    _validate_tree_path(root, path)
    before = _regular_bytes(path)
    try:
        state = json.loads(before.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise DisplayError("state_invalid") from None
    if not isinstance(state, dict):
        raise DisplayError("state_invalid")
    hashes = state.get("hashes")
    if not isinstance(hashes, dict):
        return before, before
    versions = state.get("hash_versions")
    if versions is None:
        versions = {}
        state["hash_versions"] = versions
    if not isinstance(versions, dict):
        raise DisplayError("state_invalid")
    changed = False
    for entry in entries:
        relative = str(entry["path"])
        if hashes.get(relative) == entry["legacy_before"]:
            hashes[relative] = entry["business_hash_after"]
            versions[relative] = 2
            changed = True
    if not changed:
        return before, before
    after = (json.dumps(state, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    return before, after


def _private_root(vault: Path, journal_root: Path, *, create: bool) -> Path:
    vault = vault.resolve(strict=True)
    candidate = journal_root.expanduser().absolute()
    if ".." in candidate.parts:
        raise DisplayError("journal_path_invalid")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise DisplayError("journal_path_invalid")
    if candidate.exists():
        if candidate.is_symlink() or not candidate.is_dir():
            raise DisplayError("journal_path_invalid")
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        if parent.is_symlink():
            raise DisplayError("journal_path_invalid")
        resolved = parent / candidate.name
    if resolved == vault or vault in resolved.parents or resolved in vault.parents:
        raise DisplayError("journal_overlaps_vault")
    if create and not resolved.exists():
        resolved.mkdir(mode=0o700)
    if not resolved.exists():
        raise DisplayError("journal_missing")
    os.chmod(resolved, 0o700)
    return resolved


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_private_status(path: Path, value: dict[str, object]) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise DisplayError("journal_path_invalid")
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _replace(root: Path, path: Path, expected: str | None, data: bytes, temporary_name: str) -> None:
    _validate_tree_path(root, path)
    current = _regular_bytes(path) if path.exists() else None
    current_digest = sha256(current) if current is not None else None
    if current_digest != expected:
        raise DisplayError("target_changed")
    temporary = path.parent / temporary_name
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise DisplayError("temporary_conflict")
        if sha256(_regular_bytes(temporary)) != sha256(data):
            raise DisplayError("temporary_conflict")
        temporary.unlink()
    target_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor = os.open(temporary,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), target_mode)
    try:
        os.fchmod(descriptor, target_mode)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # Narrow the non-cooperating-user window by checking immediately before
        # replace.  POSIX does not provide a content-CAS rename primitive.
        _validate_tree_path(root, path)
        latest = _regular_bytes(path) if path.exists() else None
        if (sha256(latest) if latest is not None else None) != expected:
            raise DisplayError("target_changed")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if temporary.exists() and sha256(_regular_bytes(temporary)) == sha256(data):
            temporary.unlink()
        raise


def create_plan(vault: Path, journal_root: Path) -> dict[str, object]:
    root = vault.resolve(strict=True)
    wiki_root = root / "wiki"
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        raise DisplayError("wiki_path_invalid")
    journal = _private_root(root, journal_root, create=True)
    raw = _raw_manifest(root)
    entries: list[dict[str, object]] = []
    for path in _vault_pages(root):
        _validate_tree_path(root, path)
        before = _regular_bytes(path)
        try:
            text = before.decode("utf-8")
        except UnicodeError:
            raise DisplayError("page_not_utf8") from None
        relative = path.relative_to(root).as_posix()
        after_text, _ = add_or_refresh_display(text, relative)
        after = after_text.encode("utf-8")
        before_business = business_sha256(text, relative)
        after_business = business_sha256(after_text, relative)
        before_identity = identity_sha256(text, relative)
        after_identity = identity_sha256(after_text, relative)
        before_refs = references_sha256(text, relative)
        after_refs = references_sha256(after_text, relative)
        if before_business != after_business:
            raise DisplayError("business_digest_changed")
        if (before_identity, before_refs) != (after_identity, after_refs):
            raise DisplayError("identity_reference_changed")
        entries.append({
            "path": relative,
            "before_sha256": sha256(before),
            "after_sha256": sha256(after),
            "business_sha256": before_business,
            "identity_sha256": before_identity,
            "references_sha256": before_refs,
            "legacy_before": legacy_content_hash(text),
            "business_hash_after": business_content_hash(after_text, relative),
        })
    state_before, state_after = _state_after(root, entries)
    state_entry = None
    if state_before is not None:
        state_entry = {
            "path": ".graph/state.json",
            "before_sha256": sha256(state_before),
            "after_sha256": sha256(state_after),
        }
    plan_id = uuid.uuid4().hex
    plan = {
        "format": 1,
        "plan_id": plan_id,
        "vault_key": sha256(str(root).encode("utf-8")),
        "raw": raw,
        "raw_sha256": _raw_digest(raw),
        "entries": entries,
        "state": state_entry,
        "status": "planned",
    }
    encoded = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    _write_private(journal / f"{plan_id}.json", encoded)
    _write_private_status(journal / f"{plan_id}.status.json", {
        "format": 1,
        "plan_id": plan_id,
        "state": "planned",
        "paths": {entry["path"]: "before" for entry in entries},
        "hash_state": "before" if state_entry else "absent",
    })
    return {"state": "planned", "plan_id": plan_id, "page_count": len(entries),
            "raw_sha256": plan["raw_sha256"]}


def _load_plan(vault: Path, journal_root: Path, plan_id: str) -> tuple[Path, dict[str, object]]:
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        raise DisplayError("plan_id_invalid")
    root = vault.resolve(strict=True)
    journal = _private_root(root, journal_root, create=False)
    data = _regular_bytes(journal / f"{plan_id}.json")
    try:
        plan = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise DisplayError("plan_invalid") from None
    if not isinstance(plan, dict) or (plan.get("format"), plan.get("plan_id"), plan.get("vault_key")) != (
            1, plan_id, sha256(str(root).encode("utf-8"))):
        raise DisplayError("plan_invalid")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        raise DisplayError("plan_invalid")
    seen = set()
    digest_fields = {"before_sha256": 64, "after_sha256": 64, "business_sha256": 64,
                     "identity_sha256": 64, "references_sha256": 64,
                     "legacy_before": 40, "business_hash_after": 40}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise DisplayError("plan_invalid")
        relative = entry["path"]
        path_parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in path_parts or relative in seen:
            raise DisplayError("plan_invalid")
        _page_spec(relative)
        seen.add(relative)
        for field, length in digest_fields.items():
            if not re.fullmatch(rf"[0-9a-f]{{{length}}}", str(entry.get(field, ""))):
                raise DisplayError("plan_invalid")
    state_entry = plan.get("state")
    if state_entry is not None:
        if (not isinstance(state_entry, dict) or state_entry.get("path") != ".graph/state.json"
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(state_entry.get(field, "")))
                       for field in ("before_sha256", "after_sha256"))):
            raise DisplayError("plan_invalid")
    raw_rows = plan.get("raw")
    if (not isinstance(raw_rows, list)
            or any(not isinstance(row, dict)
                   or not isinstance(row.get("path"), str)
                   or not row["path"].startswith("raw/")
                   or Path(row["path"]).is_absolute()
                   or ".." in Path(row["path"]).parts
                   or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", "")))
                   for row in raw_rows)
            or _raw_digest(raw_rows) != plan.get("raw_sha256")):
        raise DisplayError("plan_invalid")
    return journal, plan


def _require_session(root: Path) -> None:
    try:
        from wiki_session import session_is_locked
    except ImportError:
        raise DisplayError("wiki_session_missing") from None
    if not session_is_locked(root):
        raise DisplayError("vault_lock_required")


def _entry_after(root: Path, entry: dict[str, object]) -> bytes:
    path = root / str(entry["path"])
    before = _regular_bytes(path)
    text = before.decode("utf-8")
    after_text, _ = add_or_refresh_display(text, str(entry["path"]))
    return after_text.encode("utf-8")


def _backup_path(journal: Path, plan_id: str, index: int) -> Path:
    directory = journal / f"{plan_id}.backups"
    if not directory.exists():
        directory.mkdir(mode=0o700)
    if directory.is_symlink():
        raise DisplayError("journal_path_invalid")
    return directory / f"{index:05d}.bin"


def _persist_backup(path: Path, data: bytes, expected: str) -> None:
    if path.exists():
        if sha256(_regular_bytes(path)) != expected:
            raise DisplayError("backup_conflict")
        return
    _write_private(path, data)


def apply_plan(vault: Path, journal_root: Path, plan_id: str) -> dict[str, object]:
    root = vault.resolve(strict=True)
    _require_session(root)
    journal, plan = _load_plan(root, journal_root, plan_id)
    if _raw_digest(_raw_manifest(root)) != plan["raw_sha256"]:
        raise DisplayError("raw_changed")
    entries = list(plan["entries"])
    state_entry = plan.get("state")
    targets = entries + ([state_entry] if state_entry else [])
    # Complete preflight before creating a backup or touching the Vault.
    for entry in targets:
        target = root / str(entry["path"])
        _validate_tree_path(root, target)
        current = sha256(_regular_bytes(target))
        if current not in {entry["before_sha256"], entry["after_sha256"]}:
            raise DisplayError("target_changed")
    status_path = journal / f"{plan_id}.status.json"
    status = {"format": 1, "plan_id": plan_id, "state": "applying", "paths": {},
              "hash_state": "absent" if not state_entry else "before"}
    changed = 0
    for index, entry in enumerate(entries):
        path = root / str(entry["path"])
        before = _regular_bytes(path)
        current = sha256(before)
        if current == entry["after_sha256"]:
            status["paths"][entry["path"]] = "after"
            _write_private_status(status_path, status)
            continue
        _persist_backup(_backup_path(journal, plan_id, index), before, str(entry["before_sha256"]))
        after = _entry_after(root, entry)
        if sha256(after) != entry["after_sha256"]:
            raise DisplayError("plan_replay_mismatch")
        after_text = after.decode("utf-8")
        relative = str(entry["path"])
        if business_sha256(after_text, relative) != entry["business_sha256"]:
            raise DisplayError("business_digest_changed")
        if (identity_sha256(after_text, relative) != entry["identity_sha256"] or
                references_sha256(after_text, relative) != entry["references_sha256"]):
            raise DisplayError("identity_reference_changed")
        _replace(root, path, str(entry["before_sha256"]), after,
                 f".kd-display-{plan_id}-{index}.tmp")
        changed += 1
        status["paths"][entry["path"]] = "after"
        _write_private_status(status_path, status)
    if state_entry:
        state_path = root / str(state_entry["path"])
        state_current = _regular_bytes(state_path)
        if state_entry["before_sha256"] != state_entry["after_sha256"] and \
                sha256(state_current) == state_entry["before_sha256"]:
            _persist_backup(_backup_path(journal, plan_id, len(entries)), state_current,
                            str(state_entry["before_sha256"]))
            _, state_after = _state_after(root, entries)
            if state_after is None or sha256(state_after) != state_entry["after_sha256"]:
                raise DisplayError("plan_replay_mismatch")
            _replace(root, state_path, str(state_entry["before_sha256"]), state_after,
                     f".kd-display-{plan_id}-state.tmp")
            changed += 1
        status["hash_state"] = "after"
        _write_private_status(status_path, status)
    if _raw_digest(_raw_manifest(root)) != plan["raw_sha256"]:
        raise DisplayError("raw_changed")
    status["state"] = "committed"
    _write_private_status(status_path, status)
    return {"state": "committed", "plan_id": plan_id, "changed_paths": changed}


def revert_plan(vault: Path, journal_root: Path, plan_id: str) -> dict[str, object]:
    root = vault.resolve(strict=True)
    _require_session(root)
    journal, plan = _load_plan(root, journal_root, plan_id)
    if _raw_digest(_raw_manifest(root)) != plan["raw_sha256"]:
        raise DisplayError("raw_changed")
    entries = list(plan["entries"])
    state_entry = plan.get("state")
    targets = entries + ([state_entry] if state_entry else [])
    for entry in targets:
        target = root / str(entry["path"])
        _validate_tree_path(root, target)
        current = sha256(_regular_bytes(target))
        if current not in {entry["before_sha256"], entry["after_sha256"]}:
            raise DisplayError("recovery_conflict")
    status_path = journal / f"{plan_id}.status.json"
    status = {"format": 1, "plan_id": plan_id, "state": "reverting", "paths": {},
              "hash_state": "absent" if not state_entry else "after"}
    changed = 0
    # Restore hash state first.  If a process stops later, a rerun remains safe;
    # kb will never silently bless partially restored page bytes as version 2.
    ordered = []
    if state_entry:
        ordered.append((len(entries), state_entry))
    ordered.extend((index, entry) for index, entry in enumerate(entries))
    for index, entry in ordered:
        path = root / str(entry["path"])
        current = sha256(_regular_bytes(path))
        if current == entry["before_sha256"]:
            if entry is state_entry:
                status["hash_state"] = "before"
            else:
                status["paths"][entry["path"]] = "before"
            _write_private_status(status_path, status)
            continue
        backup = _backup_path(journal, plan_id, index)
        before = _regular_bytes(backup)
        if sha256(before) != entry["before_sha256"]:
            raise DisplayError("backup_conflict")
        _replace(root, path, str(entry["after_sha256"]), before,
                 f".kd-display-{plan_id}-revert-{index}.tmp")
        changed += 1
        if entry is state_entry:
            status["hash_state"] = "before"
        else:
            status["paths"][entry["path"]] = "before"
        _write_private_status(status_path, status)
    if _raw_digest(_raw_manifest(root)) != plan["raw_sha256"]:
        raise DisplayError("raw_changed")
    status["state"] = "reverted"
    _write_private_status(status_path, status)
    return {"state": "reverted", "plan_id": plan_id, "changed_paths": changed}


def _json_result(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库展示迁移（显式、可回退）")
    parser.add_argument("command", choices=["plan", "apply", "revert"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--journal-root", required=True)
    parser.add_argument("--plan-id")
    arguments = parser.parse_args()
    try:
        root = Path(arguments.root)
        journal = Path(arguments.journal_root)
        if arguments.command == "plan":
            if arguments.plan_id:
                raise DisplayError("plan_id_unexpected")
            result = create_plan(root, journal)
        else:
            if not arguments.plan_id:
                raise DisplayError("plan_id_required")
            operation = apply_plan if arguments.command == "apply" else revert_plan
            result = operation(root, journal, arguments.plan_id)
        _json_result(result)
    except (DisplayError, OSError, UnicodeError) as error:
        code = str(error) if isinstance(error, DisplayError) and str(error) else "display_io_failed"
        print(json.dumps({"state": "failed", "error_code": code}, separators=(",", ":")),
              file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
