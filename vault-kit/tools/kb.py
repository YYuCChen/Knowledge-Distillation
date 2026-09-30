#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人知识库 · 检查与关系图谱脚本

只依赖 Python 3.8+ 标准库，不调用任何 AI，任何模型维护知识库时都可以运行它。

用法（在知识库根目录运行）：
    python tools/kb.py init        初始化目录结构、七个主题页和系统页
    python tools/kb.py             完整运行：补填字段、生成图谱、重写自动内容、检查
    python tools/kb.py --dry-run   只检查，不修改任何文件
    python tools/kb.py raw-id      取一个新的 raw 素材编号（Agent 新建 AI 对话素材时用）

它做的事：
    1. 补填程序字段：编号、类型、创建、更新
    2. 解析所有页面，生成关系图谱 .graph/graph.json
    3. 计算状态：已下透 / 悬空、实践次数、素材是否已处理等
    4. 重写所有"（自动）"小节、各主题页、index.md、待确认.md
    5. 程序检查，结果写入 .graph/检查结果.md

它只读取 raw/ 中素材的信封与段落编号，永远不会修改 raw/。raw 的格式见应用仓库的
docs/engineering/raw-interface.md（守则 AGENTS.md 第 2.1 节有摘要）。
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

# ───────────────────────── 规则常量（与规则文件保持一致） ─────────────────────────

TOPICS = ["商业", "生意经", "社科", "人情世故", "AI", "投资", "健康"]

# 文件夹 → (类型, 编号前缀)
FOLDERS = {
    "来源": ("来源", "SRC"),
    "概念": ("概念", "CPT"),
    "认知": ("认知", "C"),
    "方法": ("方法", "M"),
    "技能": ("技能", "SKL"),
    "实践": ("实践", "P"),
    "综合": ("综合", "SYN"),
    "主题": ("主题", "T"),
}
TYPE_ORDER = ["认知", "方法", "技能", "实践", "概念", "来源", "综合", "主题"]

REQUIRED_FIELDS = {
    "认知": ["主题", "当前判断", "确认", "证据"],
    "方法": ["主题", "当前做法", "确认", "证据"],
    "技能": ["主题", "Skill路径", "确认"],
    "实践": ["主题", "发生日期", "生成方式", "结果", "确认"],
    "来源": ["主题", "作者", "平台", "发布日期", "素材类型", "原始文件"],
    "概念": ["主题", "子类"],
    "综合": ["主题", "触发的问题"],
    "主题": [],
}

ENUMS = {
    "确认": {"候选", "已确认"},
    "证据": {"推测", "有据", "已验证"},
    "结果": {"有效", "无效", "部分有效", "未知"},
    "生成方式": {"用户主动记录", "系统从片段推测", "系统从数据生成"},
    "子类": {"人物", "组织", "书", "术语", "现象"},
    "素材类型": {"书", "博客", "社媒", "其他"},
}

# 各类型必须具备的小节；带 * 的是自动小节
SECTIONS = {
    "认知": ["当前看法", "依据", "演化", "推论到做法*", "相关概念"],
    "方法": ["做法", "来自认知", "适用条件", "不适用条件", "演化", "落地为技能*", "实践记录*"],
    "技能": ["用途", "来自方法", "执行文件", "使用记录*"],
    "实践": ["情境", "做了什么", "结果", "事实依据", "影响"],
    "来源": ["摘要", "核心论点", "引发的想法"],
    "概念": ["定义", "各家观点", "我的相关认知*", "相关概念"],
    "综合": ["结论", "依据"],
    "主题": ["概览", "核心认知*", "核心方法*", "常用概念*", "状况*"],
}

# (页面类型, 小节) → (关系, 方向)；out = 本页 → 链接目标；in = 链接目标 → 本页
RELATIONS = {
    ("认知", "依据"): ("依据", "out"),
    ("认知", "相关概念"): ("相关", "out"),
    ("方法", "来自认知"): ("来自", "out"),
    ("方法", "不适用条件"): ("反例", "in"),
    ("技能", "来自方法"): ("落地为", "out"),
    ("实践", "做了什么"): ("实践了", "out"),
    ("实践", "影响"): ("修正", "out"),
    ("实践", "事实依据"): ("依据", "out"),
    ("来源", "引发的想法"): ("触发", "out"),
    ("概念", "相关概念"): ("相关", "out"),
    ("综合", "依据"): ("依据", "out"),
}
SKIP_EDGE_SECTIONS = {"演化"}  # 演化行单独解析为事件

AUTO_SUFFIX = "（自动）"
AUTO_MARK = "<!-- 自动生成，勿手改 -->"
CANDIDATE_MARK = "（候选）"
PAGE_CHAR_LIMIT = 1500
SKILL_IDLE_DAYS = 90

LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
RAW_RE = re.compile(r"raw/[^\s)）\]】，,；;。|]+")
RAW_ID_RE = re.compile(r"R-(\d{8})-(\d{4})")
BLOCK_RE = re.compile(r"^\^([A-Za-z0-9-]+)\s*$", re.M)
EVOLUTION_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s*\|(.*)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TODAY = dt.date.today().isoformat()


# ───────────────────────── 工具函数 ─────────────────────────

def parse_date(s):
    if not s or not DATE_RE.match(str(s).strip()):
        return None
    try:
        return dt.date.fromisoformat(str(s).strip())
    except ValueError:
        return None


def norm_title(t):
    """用于判断标题是否近似重复：去掉空白与标点，英文转小写。"""
    return re.sub(r"[\s\-_·•:：，,。.、（）()【】\[\]\"'“”‘’！!？?]", "", t).lower()


def split_frontmatter(text):
    """返回 (frontmatter 行列表或 None, 正文字符串)。"""
    if text.startswith("---"):
        lines = text.split("\n")
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], "\n".join(lines[i + 1:])
    return None, text


def parse_fm_value(v):
    v = re.sub(r"\s+#.*$", "", v).strip()
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
    return v.strip("'\"")


def parse_frontmatter(fm_lines):
    data = {}
    for line in fm_lines or []:
        m = re.match(r"^([^\s:#][^:]*):\s*(.*)$", line)
        if m:
            data[m.group(1).strip()] = parse_fm_value(m.group(2))
    return data


def set_fm_field(fm_lines, key, value):
    """在 frontmatter 行中写入字段；已存在则替换，不存在则按程序字段顺序插入。"""
    new_line = f"{key}: {value}"
    for i, line in enumerate(fm_lines):
        if re.match(rf"^{re.escape(key)}\s*:", line):
            fm_lines[i] = new_line
            return
    order = ["编号", "类型", "创建", "更新"]
    if key in order:
        pos = 0
        for i, line in enumerate(fm_lines):
            k = line.split(":", 1)[0].strip()
            if k in order and order.index(k) < order.index(key):
                pos = i + 1
        fm_lines.insert(pos, new_line)
    else:
        fm_lines.append(new_line)


def split_sections(body):
    """把正文按二级标题拆开，返回 [(标题原文 或 None, [行...]), ...]。"""
    sections, cur_head, cur = [], None, []
    for line in body.split("\n"):
        if line.startswith("## "):
            sections.append((cur_head, cur))
            cur_head, cur = line[3:].strip(), []
        else:
            cur.append(line)
    sections.append((cur_head, cur))
    return sections


def section_name(head):
    return head.replace(AUTO_SUFFIX, "").strip() if head else None


def extract_links(line):
    # [[raw/…]] 形式的原始素材引用由 extract_raw 处理，不当作页面标题。
    return [m.strip() for m in LINK_RE.findall(line) if not m.strip().startswith("raw/")]


def extract_raw(line):
    return [m.rstrip("/") for m in RAW_RE.findall(line)]


def raw_file(ref):
    """raw/…/编号.md#^source-3 → (raw/…/编号.md, source-3)。"""
    path, _, block = ref.partition("#")
    return path, block.lstrip("^") or None


def read_envelope(path):
    """raw 素材信封中的单行字段（编号、身份、标题、取代等）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}, set()
    fm, body = split_frontmatter(text)
    meta = {}
    for line in fm or []:
        m = re.match(r"^([^\s:#-][^:]*):\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value.startswith('"'):
            # 应用把自由文本写成 JSON 形式的双引号字符串（合法 YAML）。
            try:
                value = json.loads(value)
            except ValueError:
                value = value.strip('"')
        meta[m.group(1).strip()] = value
    return meta, set(BLOCK_RE.findall(body))


def visible_length(body_lines):
    text = "\n".join(body_lines)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"[#>*`\-\[\]|]", "", text)
    return len(re.sub(r"\s", "", text))


# ───────────────────────── 页面模型 ─────────────────────────

class Page:
    def __init__(self, path, root):
        self.path = path
        self.rel = path.relative_to(root).as_posix()
        self.title = path.stem
        self.folder = path.parent.name
        self.type, self.prefix = FOLDERS[self.folder]
        self.text = path.read_text(encoding="utf-8")
        fm, self.body = split_frontmatter(self.text)
        self.has_fm = fm is not None
        self.fm_lines = list(fm) if fm is not None else []
        self.meta = parse_frontmatter(self.fm_lines)
        self.sections = split_sections(self.body)
        self.dirty = False

    # 当前元数据中的常用字段
    def get(self, key, default=""):
        return self.meta.get(key, default)

    @property
    def topics(self):
        t = self.meta.get("主题", [])
        return t if isinstance(t, list) else [t] if t else []

    @property
    def confirmed(self):
        return self.get("确认") == "已确认"

    def content_hash(self):
        """用于判断内容是否真正改变：忽略程序字段与自动小节。"""
        fm = [l for l in self.fm_lines
              if l.split(":", 1)[0].strip() not in ("编号", "类型", "创建", "更新")]
        parts = ["\n".join(fm)]
        for head, lines in self.sections:
            if head and head.endswith(AUTO_SUFFIX):
                parts.append(head)
            else:
                parts.append((head or "") + "\n" + "\n".join(lines))
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

    def render(self):
        out = []
        if self.has_fm or self.fm_lines:
            out += ["---"] + self.fm_lines + ["---"]
        body_lines = []
        for head, lines in self.sections:
            if head is not None:
                body_lines.append("## " + head)
            body_lines += lines
        body = "\n".join(body_lines)
        return ("\n".join(out) + "\n" + body) if out else body

    def set_auto_section(self, name, content_lines):
        head = name + AUTO_SUFFIX
        new = [AUTO_MARK] + content_lines + [""]
        for i, (h, lines) in enumerate(self.sections):
            if h and section_name(h) == name:
                if h != head or lines != new:
                    self.sections[i] = (head, new)
                    self.dirty = True
                return
        # 缺失则追加到末尾
        if self.sections and self.sections[-1][1] and self.sections[-1][1][-1] != "":
            self.sections[-1][1].append("")
        self.sections.append((head, new))
        self.dirty = True

    def section_lines(self, name):
        for h, lines in self.sections:
            if h and section_name(h) == name:
                return lines
        return []

    def first_line(self, name):
        for line in self.section_lines(name):
            s = line.strip().lstrip("-").strip()
            if s and not s.startswith("<!--"):
                return s
        return ""


# ───────────────────────── 知识库 ─────────────────────────

class Vault:
    def __init__(self, root):
        self.root = root
        self.wiki = root / "wiki"
        self.raw = root / "raw"
        self.gdir = root / ".graph"
        self.pages = []
        self.by_title = {}
        self.edges = []      # dict(从, 到, 关系, 候选, 附注)
        self.events = []     # dict(页面, 日期, 动作, 内容, 起因, 状态)
        self.issues = []     # (级别, 类别, 说明)
        self.changes = {"补填字段": 0, "更新时间": 0, "重写自动内容": 0}
        self.raws = {}       # 路径 → (信封, 段落编号集合)
        self.superseded = {} # 被取代的编号 → 取代它的文件路径

    def load_raw(self):
        """读取 raw/ 中每份素材的信封与段落编号；取代关系以最新文件为准。"""
        if not self.raw.is_dir():
            return
        by_id = {}
        for f in sorted(self.raw.rglob("*.md")):
            rel = f.relative_to(self.root).as_posix()
            meta, blocks = read_envelope(f)
            self.raws[rel] = (meta, blocks)
            if meta.get("编号"):
                by_id.setdefault(meta["编号"], []).append(rel)
        for rid, paths in by_id.items():
            if len(paths) > 1:
                self.issue("错误", "素材编号重复", f"编号 {rid} 同时用于 " + "、".join(paths))
        for rel, (meta, _) in self.raws.items():
            old = meta.get("取代")
            if old:
                self.superseded[old] = rel
                if old not in by_id:
                    self.issue("提醒", "取代对象不存在", f"{rel} 取代的 {old} 不在 raw/ 中")

    def current_raw(self, rel):
        """沿取代关系找到最新的一份素材。"""
        seen = set()
        meta = self.raws.get(rel, ({}, set()))[0]
        while meta.get("编号") in self.superseded and rel not in seen:
            seen.add(rel)
            rel = self.superseded[meta["编号"]]
            meta = self.raws.get(rel, ({}, set()))[0]
        return rel

    # ── 读取 ──
    def load(self):
        for folder in FOLDERS:
            d = self.wiki / folder
            if d.is_dir():
                for p in sorted(d.glob("*.md")):
                    self.pages.append(Page(p, self.root))
        seen_exact, seen_norm = {}, {}
        for pg in self.pages:
            if pg.title in seen_exact:
                self.issue("错误", "重复标题",
                           f"[[{pg.title}]] 同时存在于 {seen_exact[pg.title]} 和 {pg.rel}")
            else:
                seen_exact[pg.title] = pg.rel
                self.by_title[pg.title] = pg
            n = norm_title(pg.title)
            if n in seen_norm and seen_norm[n] != pg.title:
                self.issue("提醒", "近似标题",
                           f"[[{seen_norm[n]}]] 与 [[{pg.title}]] 可能在回答同一个问题")
            seen_norm.setdefault(n, pg.title)

    def issue(self, level, kind, msg):
        self.issues.append((level, kind, msg))

    # ── 补填程序字段 ──
    def fill_program_fields(self, state):
        used = {}
        for pg in self.pages:
            m = re.match(r"^([A-Z]+)-(\d+)$", str(pg.get("编号")))
            if m:
                used.setdefault(m.group(1), set()).add(int(m.group(2)))
        hashes = state.setdefault("hashes", {})
        for pg in self.pages:
            before = list(pg.fm_lines)
            if not pg.get("编号"):
                n = max(used.get(pg.prefix, {0}) | {0}) + 1
                used.setdefault(pg.prefix, set()).add(n)
                set_fm_field(pg.fm_lines, "编号", f"{pg.prefix}-{n:04d}")
            if pg.get("类型") != pg.type:
                set_fm_field(pg.fm_lines, "类型", pg.type)
            if not parse_date(pg.get("创建")):
                set_fm_field(pg.fm_lines, "创建", TODAY)
            h = pg.content_hash()
            old = hashes.get(pg.rel)
            if not parse_date(pg.get("更新")):
                set_fm_field(pg.fm_lines, "更新", TODAY)
            elif old is not None and old != h and pg.get("更新") != TODAY:
                set_fm_field(pg.fm_lines, "更新", TODAY)
                self.changes["更新时间"] += 1
            hashes[pg.rel] = h
            if pg.fm_lines != before:
                pg.has_fm = True
                pg.meta = parse_frontmatter(pg.fm_lines)
                pg.dirty = True
                self.changes["补填字段"] += 1

    # ── 解析关系与事件 ──
    def build_graph(self):
        for pg in self.pages:
            for head, lines in pg.sections:
                name = section_name(head)
                if head and head.endswith(AUTO_SUFFIX):
                    continue  # 自动小节是派生内容，不作为关系来源
                if name in SKIP_EDGE_SECTIONS:
                    self.parse_events(pg, lines)
                    continue
                rel, direction = RELATIONS.get((pg.type, name), ("提及", "out"))
                for line in lines:
                    cand = CANDIDATE_MARK in line
                    note = line.split("]]")[-1].strip(" ：:-") if "]]" in line else ""
                    targets = extract_links(line) + extract_raw(line)
                    for t in targets:
                        if t == pg.title:
                            continue
                        e = {"从": pg.title, "到": t, "关系": rel, "候选": cand,
                             "附注": note, "所在": f"{pg.title}·{name or '正文前'}"}
                        if direction == "in":
                            e["从"], e["到"] = t, pg.title
                        self.edges.append(e)
            # 元数据里的原始文件路径
            src = pg.get("原始文件")
            if pg.type == "来源" and src:
                self.edges.append({"从": pg.title, "到": src, "关系": "依据",
                                   "候选": False, "附注": "", "所在": f"{pg.title}·元数据"})

    def parse_events(self, pg, lines):
        for line in lines:
            m = EVOLUTION_RE.match(line)
            if not m:
                if line.strip().startswith("-"):
                    self.issue("提醒", "演化格式", f"[[{pg.title}]] 演化记录格式不符：{line.strip()}")
                continue
            parts = [p.strip() for p in m.group(2).split("|")]
            action = parts[0] if parts else ""
            content = parts[1] if len(parts) > 1 else ""
            cause = parts[2] if len(parts) > 2 else ""
            status = parts[3] if len(parts) > 3 else ""
            if status not in ENUMS["确认"]:
                self.issue("提醒", "演化格式", f"[[{pg.title}]] 演化记录缺少状态（候选/已确认）：{line.strip()}")
            self.events.append({"页面": pg.title, "日期": m.group(1), "动作": action,
                                "内容": content, "起因": extract_links(cause) + extract_raw(cause),
                                "状态": status})

    # ── 查询辅助 ──
    def in_edges(self, title, rel=None):
        return [e for e in self.edges if e["到"] == title and (rel is None or e["关系"] == rel)]

    def out_edges(self, title, rel=None):
        return [e for e in self.edges if e["从"] == title and (rel is None or e["关系"] == rel)]

    def page(self, title):
        return self.by_title.get(title)

    # ── 计算状态 ──
    def compute_states(self):
        self.state = {}
        for pg in self.pages:
            s = {}
            if pg.type == "认知":
                methods = [self.page(e["从"]) for e in self.in_edges(pg.title, "来自")]
                methods = [m for m in methods if m and m.type == "方法"]
                s["方法数"] = len([m for m in methods if m.confirmed])
                if pg.confirmed:
                    s["下透"] = "已下透" if s["方法数"] else "悬空"
            if pg.type in ("方法", "技能"):
                practices = [self.page(e["从"]) for e in self.in_edges(pg.title, "实践了")]
                practices = [p for p in practices if p and p.type == "实践" and p.confirmed]
                dates = [parse_date(p.get("发生日期")) for p in practices]
                dates = [d for d in dates if d]
                s["实践次数"] = len(practices)
                s["最近实践"] = max(dates).isoformat() if dates else ""
                s["有效次数"] = len([p for p in practices if p.get("结果") == "有效"])
                if practices:
                    latest = max(practices, key=lambda p: p.get("发生日期", ""))
                    s["最近结果"] = latest.get("结果")
            self.state[pg.title] = s

    # ── 重写自动内容 ──
    def fmt_line(self, pg, extra=""):
        tag = "" if pg.confirmed or pg.type in ("来源", "概念", "综合", "主题") else "（候选）"
        return f"- [[{pg.title}]]{(' —— ' + extra) if extra else ''}{tag}"

    def regenerate(self):
        for pg in self.pages:
            before = pg.render()
            if pg.type == "认知":
                ms = self.linked_pages(self.in_edges(pg.title, "来自"), "方法")
                pg.set_auto_section("推论到做法", [self.fmt_line(m, m.get("当前做法")) for m in ms])
            elif pg.type == "方法":
                sk = self.linked_pages(self.in_edges(pg.title, "落地为"), "技能")
                pg.set_auto_section("落地为技能", [self.fmt_line(k, k.first_line("用途")) for k in sk])
                ps = self.linked_pages(self.in_edges(pg.title, "实践了"), "实践")
                pg.set_auto_section("实践记录", [self.fmt_line(p, f"{p.get('发生日期')} · {p.get('结果')}")
                                              for p in sorted(ps, key=lambda p: p.get("发生日期", ""))])
            elif pg.type == "技能":
                ps = self.linked_pages(self.in_edges(pg.title, "实践了"), "实践")
                pg.set_auto_section("使用记录", [self.fmt_line(p, f"{p.get('发生日期')} · {p.get('结果')}")
                                              for p in sorted(ps, key=lambda p: p.get("发生日期", ""))])
            elif pg.type == "概念":
                cs = self.linked_pages(self.in_edges(pg.title, "相关"), "认知")
                pg.set_auto_section("我的相关认知", [self.fmt_line(c, c.get("当前判断")) for c in cs])
            elif pg.type == "主题":
                self.regenerate_topic(pg)
            if pg.render() != before:
                self.changes["重写自动内容"] += 1

    def linked_pages(self, edges, ptype):
        out, seen = [], set()
        for e in edges:
            p = self.page(e["从"])
            if p and p.type == ptype and p.title not in seen:
                seen.add(p.title)
                out.append(p)
        return out

    def regenerate_topic(self, tp):
        name = tp.title
        pages = [p for p in self.pages if name in p.topics]
        cogs = [p for p in pages if p.type == "认知" and p.confirmed]
        meths = [p for p in pages if p.type == "方法" and p.confirmed]
        concepts = [p for p in pages if p.type == "概念"]
        concepts.sort(key=lambda c: -len(self.in_edges(c.title)))
        tp.set_auto_section("核心认知", [f"- [[{c.title}]] —— {c.get('当前判断')}" for c in cogs] or ["（暂无）"])
        tp.set_auto_section("核心方法", [f"- [[{m.title}]] —— {m.get('当前做法')}" for m in meths] or ["（暂无）"])
        tp.set_auto_section("常用概念", [f"- [[{c.title}]]" for c in concepts[:20]] or ["（暂无）"])
        counts = {t: len([p for p in pages if p.type == t]) for t in TYPE_ORDER if t != "主题"}
        dangling = [c for c in cogs if self.state[c.title].get("下透") == "悬空"]
        blind = [c for c in concepts if not self.in_edges(c.title, "相关")]
        status = ["- 页面数量：" + "，".join(f"{k} {v}" for k, v in counts.items())]
        status.append("- 悬空认知（仅供参考）：" + ("、".join(f"[[{c.title}]]" for c in dangling) or "无"))
        status.append("- 只有外部观点的概念：" + ("、".join(f"[[{c.title}]]" for c in blind) or "无"))
        tp.set_auto_section("状况", status)

    # ── 检查 ──
    def check(self):
        titles = set(self.by_title)
        for e in self.edges:
            t = e["到"] if e["从"] in titles else e["从"]
            if t.startswith("raw/"):
                path, block = raw_file(t)
                if not (self.root / path).exists():
                    self.issue("错误", "原始文件不存在", f"{e['所在']} 引用的 {path} 不存在")
                elif block and path in self.raws and block not in self.raws[path][1]:
                    self.issue("错误", "段落不存在", f"{e['所在']} 引用的 {t} 在该素材中没有段落 ^{block}")
                elif path in self.raws and self.current_raw(path) != path:
                    self.issue("提醒", "引用了被取代的素材",
                               f"{e['所在']} 引用的 {path} 已被 {self.current_raw(path)} 取代，以最新文件为准")
            elif t not in titles:
                self.issue("错误", "断链", f"{e['所在']} 链接到不存在的页面 [[{t}]]")

        for pg in self.pages:
            # 标题字符：跨系统同步时这些字符会出问题
            if re.search(r'[\\/:*?"<>|]', pg.title):
                self.issue("错误", "标题字符", f"[[{pg.title}]] 含有不能用于文件名的字符，冒号请用全角「：」")
            # 元数据
            if not pg.has_fm:
                self.issue("错误", "元数据缺失", f"[[{pg.title}]] 没有元数据")
            for f in REQUIRED_FIELDS[pg.type]:
                if not pg.meta.get(f):
                    self.issue("错误", "元数据缺失", f"[[{pg.title}]] 缺少字段「{f}」")
            for f, allowed in ENUMS.items():
                v = pg.meta.get(f)
                if v and v not in allowed:
                    self.issue("错误", "元数据取值", f"[[{pg.title}]] 字段「{f}」取值「{v}」不合法，应为 {'/'.join(sorted(allowed))}")
            for t in pg.topics:
                if t not in TOPICS:
                    self.issue("错误", "元数据取值", f"[[{pg.title}]] 主题「{t}」不在七个主题之内")
            if pg.prefix and pg.get("编号") and not str(pg.get("编号")).startswith(pg.prefix + "-"):
                self.issue("错误", "编号前缀", f"[[{pg.title}]] 编号 {pg.get('编号')} 与类型 {pg.type} 不符")
            for f in ("创建", "更新", "发生日期", "发布日期"):
                v = pg.meta.get(f)
                if f == "发布日期" and v == "未知":
                    continue  # 粘贴的文字、文件常常没有发布日期（raw 信封无「产生于」）
                if v and not parse_date(v):
                    self.issue("提醒", "日期格式", f"[[{pg.title}]] 字段「{f}」应为 年-月-日 格式：{v}")

            # 小节
            present = [section_name(h) for h, _ in pg.sections if h]
            for s in SECTIONS[pg.type]:
                if s.rstrip("*") not in present:
                    self.issue("错误", "小节缺失", f"[[{pg.title}]] 缺少小节「{s.rstrip('*')}」")
            allowed = {s.rstrip("*") for s in SECTIONS[pg.type]}
            for s in present:
                if s not in allowed:
                    self.issue("提醒", "多余小节", f"[[{pg.title}]] 有模板之外的小节「{s}」，其中的链接只按「提及」处理")

            # 长度
            if pg.type != "主题":
                body_lines = [l for h, ls in pg.sections if not (h and h.endswith(AUTO_SUFFIX)) for l in ls]
                n = visible_length(body_lines)
                if n > PAGE_CHAR_LIMIT:
                    self.issue("提醒", "页面过长", f"[[{pg.title}]] 约 {n} 字，超过 {PAGE_CHAR_LIMIT} 字，建议拆页")

            # 孤儿页：与任何页面都没有关系（进出都算）
            if pg.type != "主题" and not self.in_edges(pg.title) and not self.out_edges(pg.title):
                self.issue("提醒", "孤儿页", f"[[{pg.title}]] 与其他页面没有任何链接关系")

            # 认知页依据必须包含用户自己的来源
            if pg.type == "认知":
                basis = self.out_edges(pg.title, "依据")
                own = [e for e in basis if e["到"].startswith("raw/自述/")
                       or (self.page(e["到"]) and self.page(e["到"]).type == "实践"
                           and self.page(e["到"]).confirmed)]
                if not own:
                    self.issue("错误", "他人与自己", f"[[{pg.title}]] 的依据中没有用户片段或已确认的实践记录")

            # 从严主题
            if set(pg.topics) & {"投资", "健康"} and pg.get("证据") == "推测":
                self.issue("提醒", "从严主题", f"[[{pg.title}]] 属于投资/健康主题，但证据等级为推测")

            # 方法页条件
            if pg.type == "方法":
                for s in ("适用条件", "不适用条件"):
                    if not "".join(pg.section_lines(s)).strip():
                        self.issue("提醒", "条件未填", f"[[{pg.title}]] 的「{s}」为空，至少写「待补充」")

        # 综合页不得作为依据
        for e in self.edges:
            if e["关系"] in ("依据", "反例"):
                src = self.page(e["到"] if e["关系"] == "依据" else e["从"])
                if src and src.type == "综合":
                    self.issue("错误", "综合页作依据", f"{e['所在']} 把综合页 [[{src.title}]] 当作依据")

        # 下透相关
        today = dt.date.today()
        for pg in self.pages:
            s = self.state.get(pg.title, {})
            if pg.type == "方法" and pg.confirmed and s.get("实践次数", 0) == 0:
                self.issue("信息", "未实践的方法", f"[[{pg.title}]] 还没有已确认的实践记录")
            if pg.type == "方法" and s.get("最近结果") == "无效":
                last = s.get("最近实践", "")
                revised = [ev for ev in self.events if ev["页面"] == pg.title and ev["日期"] >= last]
                if not revised:
                    self.issue("提醒", "被推翻未修正", f"[[{pg.title}]] 最近一次实践结果无效，之后没有修订记录")
            if pg.type == "技能" and pg.confirmed:
                last = parse_date(s.get("最近实践"))
                if not last or (today - last).days > SKILL_IDLE_DAYS:
                    self.issue("信息", "长期不用的技能", f"[[{pg.title}]] 超过 {SKILL_IDLE_DAYS} 天没有使用记录")
            if pg.type == "认知" and s.get("下透") == "悬空":
                self.issue("信息", "悬空认知", f"[[{pg.title}]] 尚未推导出已确认的方法（仅供参考）")

        # 待处理素材：去掉 #^段落 后比较；被取代的旧文件不再列出，以最新文件为准
        self.pending = {"外部": [], "自述": []}
        referenced = {raw_file(x)[0] for e in self.edges for x in (e["从"], e["到"]) if x.startswith("raw/")}
        log_text = (self.wiki / "log.md").read_text(encoding="utf-8") if (self.wiki / "log.md").exists() else ""
        for kind in ("外部", "自述"):
            d = self.raw / kind
            if d.is_dir():
                for f in sorted(d.rglob("*.md")):
                    rel = f.relative_to(self.root).as_posix()
                    if self.current_raw(rel) != rel:
                        continue
                    if rel not in referenced and rel not in log_text:
                        self.pending[kind].append(rel)

        # 查询覆盖
        self.never_read = []
        qf = self.gdir / "queries.jsonl"
        if qf.exists() and qf.stat().st_size:
            read = set()
            for line in qf.read_text(encoding="utf-8").splitlines():
                try:
                    read |= set(json.loads(line).get("页面", []))
                except json.JSONDecodeError:
                    continue
            self.never_read = [p.title for p in self.pages if p.type != "主题" and p.title not in read]

    # ── 系统页 ──
    def write_index(self):
        lines = ["# 索引", "", AUTO_MARK, f"更新于 {TODAY}，由 tools/kb.py 生成。", ""]
        for t in TYPE_ORDER:
            group = [p for p in self.pages if p.type == t]
            if t == "主题":
                group.sort(key=lambda p: TOPICS.index(p.title) if p.title in TOPICS else 99)
            if not group:
                continue
            lines += [f"## {t}"]
            for p in group:
                lines.append(self.index_line(p))
            lines.append("")
        return "\n".join(lines)

    def index_line(self, p):
        s = self.state.get(p.title, {})
        conf = p.get("确认")
        if p.type == "认知":
            tags = [conf] + ([s["下透"]] if s.get("下透") else [])
            return f"- [[{p.title}]] —— 当前判断：{p.get('当前判断')}（{' · '.join(filter(None, tags))}）"
        if p.type == "方法":
            return f"- [[{p.title}]] —— 当前做法：{p.get('当前做法')}（{conf} · 实践 {s.get('实践次数', 0)} 次）"
        if p.type == "技能":
            return f"- [[{p.title}]] —— {p.first_line('用途')}（{conf} · 使用 {s.get('实践次数', 0)} 次）"
        if p.type == "实践":
            return f"- [[{p.title}]] —— {p.get('发生日期')} · {p.get('结果')}（{conf}）"
        if p.type == "概念":
            return f"- [[{p.title}]] —— {p.first_line('定义')}"
        if p.type == "来源":
            return f"- [[{p.title}]] —— {p.first_line('摘要')}"
        if p.type == "综合":
            return f"- [[{p.title}]] —— 问题：{p.get('触发的问题')}"
        return f"- [[{p.title}]]"

    def write_pending(self):
        items = []
        for p in self.pages:
            if p.get("确认") == "候选":
                label = {"认知": "新认知", "方法": "新方法", "技能": "新技能", "实践": "实践记录"}.get(p.type)
                if not label:
                    continue
                if p.type == "认知":
                    detail = [f"推断：{p.get('当前判断')}",
                              "依据：" + "、".join(e["到"] if e["到"].startswith("raw/") else f"[[{e['到']}]]"
                                                  for e in self.out_edges(p.title, "依据"))]
                elif p.type == "方法":
                    detail = [f"推断：{p.get('当前做法')}"]
                elif p.type == "实践":
                    used = "、".join(f"[[{e['到']}]]" for e in self.out_edges(p.title, "实践了"))
                    detail = [f"推断：{p.get('发生日期')} 使用了 {used or '（未关联方法）'}，结果{p.get('结果')}"]
                else:
                    detail = [f"用途：{p.first_line('用途')}"]
                items.append((p.get("创建", ""), f"{label} · {p.title}", detail))
        for ev in self.events:
            p = self.page(ev["页面"])
            if ev["状态"] == "候选" and p and p.confirmed:
                label = "认知修订" if p.type == "认知" else "方法修订"
                items.append((ev["日期"], f"{label} · {p.title}",
                              [f"原：{p.get('当前判断') or p.get('当前做法')}",
                               f"改为：{ev['内容']}",
                               "起因：" + "、".join(ev["起因"])]))
        for e in self.edges:
            if e["候选"] and e["关系"] in ("触发", "反例"):
                label = "触发源关联" if e["关系"] == "触发" else "反例"
                items.append(("", f"{label} · {e['从']} → {e['到']}", [f"位置：{e['所在']}"]))
        items.sort(key=lambda x: x[0])
        lines = ["# 待确认清单", "", AUTO_MARK,
                 f"更新于 {TODAY}，由 tools/kb.py 根据页面中的候选内容生成。", ""]
        if not items:
            lines.append("暂无待确认事项。")
        for i, (_, head, detail) in enumerate(items, 1):
            lines.append(f"[{i}] {head}")
            lines += [f"    {d}" for d in detail]
            lines += ["    → 对 / 不对 / 改成……", ""]
        return "\n".join(lines)

    def write_report(self):
        order = {"错误": 0, "提醒": 1, "信息": 2}
        issues = sorted(set(self.issues), key=lambda x: (order[x[0]], x[1], x[2]))
        lines = [f"# 检查结果 {TODAY}", "",
                 f"页面 {len(self.pages)} 个，关系 {len(self.edges)} 条，演化事件 {len(self.events)} 条。", "",
                 "## 本次修改", ""]
        lines += [f"- {k}：{v} 页" for k, v in self.changes.items()] + [""]
        for level in ("错误", "提醒", "信息"):
            group = [x for x in issues if x[0] == level]
            lines += [f"## {level}（{len(group)}）", ""]
            lines += [f"- 【{k}】{m}" for _, k, m in group] or ["- 无"]
            lines.append("")
        lines += ["## 待处理素材", ""]
        for kind, files in self.pending.items():
            lines.append(f"- {kind}：{len(files)} 份")
            for f in files[:50]:
                meta = self.raws.get(f, ({}, set()))[0]
                extra = " · ".join(filter(None, [meta.get("身份"), meta.get("渠道"), meta.get("标题")]))
                note = f"（取代 {meta['取代']}）" if meta.get("取代") else ""
                lines.append(f"  - {f}" + (f" · {extra}" if extra else "") + note)
        if self.never_read:
            lines += ["", "## 从未被查询读取的页面", ""] + [f"- [[{t}]]" for t in self.never_read]
        return "\n".join(lines) + "\n", issues

    def graph_json(self):
        nodes = []
        for p in self.pages:
            nodes.append({"标题": p.title, "编号": p.get("编号"), "类型": p.type, "路径": p.rel,
                          "主题": p.topics, "确认": p.get("确认"),
                          "当前判断": p.get("当前判断") or p.get("当前做法"),
                          "状态": self.state.get(p.title, {})})
        raws = sorted({raw_file(x)[0] for e in self.edges for x in (e["从"], e["到"]) if x.startswith("raw/")})
        for r in raws:
            meta = self.raws.get(r, ({}, set()))[0]
            nodes.append({"标题": meta.get("标题") or r, "类型": "原始", "路径": r, "编号": meta.get("编号"),
                          "身份": meta.get("身份"), "取代者": self.current_raw(r) if self.current_raw(r) != r else None})
        return {"生成时间": dt.datetime.now().isoformat(timespec="seconds"),
                "节点": nodes, "边": self.edges, "事件": self.events}


# ───────────────────────── 命令 ─────────────────────────

TOPIC_TEMPLATE = """---
类型: 主题
---

## 概览
（由 AI 每周根据本主题下已确认的认知和方法重写）

## 核心认知（自动）
## 核心方法（自动）
## 常用概念（自动）
## 状况（自动）
"""


def cmd_init(root):
    for d in ["raw/外部", "raw/自述", "raw/数据", "skills", ".graph"] + [f"wiki/{f}" for f in FOLDERS]:
        (root / d).mkdir(parents=True, exist_ok=True)
    for t in TOPICS:
        p = root / "wiki" / "主题" / f"{t}.md"
        if not p.exists():
            p.write_text(TOPIC_TEMPLATE, encoding="utf-8")
    for name, content in [("log.md", "# 变更日志\n"), ("体检报告.md", "# 体检报告\n\n尚未运行每周检查。\n"),
                          ("index.md", "# 索引\n"), ("待确认.md", "# 待确认清单\n")]:
        p = root / "wiki" / name
        if not p.exists():
            p.write_text(content, encoding="utf-8")
    print("已初始化目录结构、七个主题页和系统页。接着运行：python tools/kb.py")


def cmd_run(root, dry):
    v = Vault(root)
    if not v.wiki.is_dir():
        sys.exit("找不到 wiki/ 目录。请在知识库根目录运行，或先运行：python tools/kb.py init")
    state_file = v.gdir / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}

    v.load()
    v.load_raw()
    v.fill_program_fields(state)
    v.build_graph()
    v.compute_states()
    v.regenerate()
    v.check()
    report, issues = v.write_report()

    if dry:
        print(report)
        return

    written = 0
    for pg in v.pages:
        if pg.dirty:
            new = pg.render()
            if new != pg.text:
                pg.path.write_text(new, encoding="utf-8")
                written += 1
    # 自动小节改动不应让"更新"字段变化：写回后重新记录哈希
    state["hashes"] = {pg.rel: pg.content_hash() for pg in v.pages}
    (v.wiki / "index.md").write_text(v.write_index(), encoding="utf-8")
    (v.wiki / "待确认.md").write_text(v.write_pending(), encoding="utf-8")
    v.gdir.mkdir(exist_ok=True)
    (v.gdir / "graph.json").write_text(json.dumps(v.graph_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    (v.gdir / "检查结果.md").write_text(report, encoding="utf-8")
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    errors = len([x for x in issues if x[0] == "错误"])
    warns = len([x for x in issues if x[0] == "提醒"])
    if written:
        with open(v.wiki / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n## [{TODAY}] check | 程序检查\n- 修改页面 {written} 个；错误 {errors} 条，提醒 {warns} 条\n")
    print(f"完成：页面 {len(v.pages)} 个，关系 {len(v.edges)} 条，修改 {written} 个文件；"
          f"错误 {errors} 条，提醒 {warns} 条。详见 .graph/检查结果.md")
    sys.exit(1 if errors else 0)


def next_raw_id(root, day=None):
    """当天最大号加一：扫描 raw/ 下已有文件名（应用写入的与 Agent 写入的都算）。"""
    day = day or dt.date.today()
    stamp = day.strftime("%Y%m%d")
    highest = 0
    raw = root / "raw"
    if raw.is_dir():
        for f in raw.rglob(f"R-{stamp}-*.md"):
            m = RAW_ID_RE.fullmatch(f.stem)
            if m:
                highest = max(highest, int(m.group(2)))
    if highest >= 9999:
        sys.exit(f"{stamp} 的编号已用完")
    return f"R-{stamp}-{highest + 1:04d}"


def cmd_raw_id(root, date_text):
    day = dt.date.fromisoformat(date_text) if date_text else None
    rid = next_raw_id(root, day)
    d = day or dt.date.today()
    print(rid)
    print(f"新建文件：raw/自述/{d:%Y}/{d:%m}/{rid}.md（只新建，不修改已有文件）", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="个人知识库检查与关系图谱脚本")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "init", "raw-id"])
    ap.add_argument("--root", default=None, help="知识库根目录，默认为脚本所在目录的上一级")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不修改文件")
    ap.add_argument("--date", default=None, help="raw-id：取号日期，年-月-日，默认今天")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if args.command == "init":
        cmd_init(root)
    elif args.command == "raw-id":
        cmd_raw_id(root, args.date)
    else:
        cmd_run(root, args.dry_run)


if __name__ == "__main__":
    main()
