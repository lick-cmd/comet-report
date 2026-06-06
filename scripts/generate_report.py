#!/usr/bin/env python3
"""Generate structured Comet workflow reports (Markdown + HTML) for an OpenSpec change."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_OUTPUT_ROOT = Path.home() / "Documents" / "CometReport"
CONFIG_FILENAMES = (".comet-report.yaml", ".comet-report.yml")
COMET_PHASES = ["open", "design", "build", "verify", "archive"]
ROI_BENEFIT = ["高", "中高", "中", "低中", "低"]


@dataclass
class TaskItem:
    section: str
    id: str
    text: str
    done: bool

    @property
    def short_title(self) -> str:
        body = re.sub(r"^\d+(?:\.\d+)*\s+", "", self.text)
        for sep in ("：", ":", "—", "-"):
            if sep in body:
                return body.split(sep, 1)[0].strip()
        return body[:40] + ("…" if len(body) > 40 else "")

    @property
    def detail(self) -> str:
        body = re.sub(r"^\d+(?:\.\d+)*\s+", "", self.text)
        if "：" in body:
            return body.split("：", 1)[1].strip()
        if ":" in body:
            return body.split(":", 1)[1].strip()
        return body

    @property
    def verify_hint(self) -> str:
        low = self.text.lower()
        if any(k in self.text for k in ("P95", "P99", "APM", "灰度", "压测")):
            return "P95/P99 观察"
        if any(k in self.text for k in ("单测", "对比测试", "一致")):
            return "行为/数据一致"
        if any(k in self.text for k in ("采样", "耗时", "命中率", "日志")):
            return "指标/日志"
        if any(k in self.text for k in ("评审", "方案")):
            return "方案确认"
        if any(k in self.text for k in ("基线", "记录")):
            return "基线采集"
        if "php -l" in low or "语法" in self.text:
            return "无错误"
        return "—"

    def status_label(self) -> tuple[str, str]:
        """Return (label, css_class) for HTML tags."""
        if self.done:
            return "完成", "tag-done"
        if re.search(r"验证|发布|灰度|压测|APM|对比测试", self.section + self.text):
            return "待验证", "tag-wait"
        return "未做", "tag-pending"


@dataclass
class SpecRequirement:
    name: str
    scenarios: list[str]


@dataclass
class ReportContext:
    change: str
    repo_root: Path
    change_dir: Path
    archived: bool
    workflow: str = ""
    phase: str = ""
    design_doc: str = ""
    plan: str = ""
    verify_report: str = ""
    verify_result: str = ""
    verify_mode: str = ""
    handoff_hash: str = ""
    base_ref: str = ""
    task_done: int = 0
    task_total: int = 0
    tasks: list[TaskItem] = field(default_factory=list)
    openspec_status: Optional[dict] = None
    generated_at: str = ""
    endpoint: str = ""
    git_diff: str = ""


def esc(text: str) -> str:
    return html.escape(text)


def strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / "openspec").is_dir():
            return cur
        cur = cur.parent
    raise SystemExit("ERROR: openspec/ not found. Run from repository root.")


def parse_flat_yaml(text: str) -> dict[str, str]:
    """Parse simple key: value YAML without external dependencies."""
    config: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.startswith("#"):
            value = value.split("#", 1)[0].strip()
        if key:
            config[key] = value
    return config


def load_report_config(repo_root: Path) -> dict[str, str]:
    for name in CONFIG_FILENAMES:
        path = repo_root / name
        if path.is_file():
            return parse_flat_yaml(path.read_text(encoding="utf-8"))
    return {}


def is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_output_dir(
    repo_root: Path,
    change: str,
    timestamp: str,
    cli_output_dir: Optional[str],
) -> Path:
    """
    Resolve report output directory.

    Priority:
    1. CLI --output-dir / -o  → exact directory
    2. COMET_REPORT_DIR env    → root + {change}-{timestamp}
    3. .comet-report.yaml      → output_dir (+ optional append_timestamp)
    4. DEFAULT_OUTPUT_ROOT     → root + {change}-{timestamp}
    """
    if cli_output_dir:
        return Path(cli_output_dir).expanduser().resolve()

    env_root = os.environ.get("COMET_REPORT_DIR", "").strip()
    if env_root:
        root = Path(env_root).expanduser()
        if not root.is_absolute():
            root = (repo_root / root).resolve()
        return (root / f"{change}-{timestamp}").resolve()

    config = load_report_config(repo_root)
    configured = config.get("output_dir", "").strip()
    if configured:
        out = Path(configured).expanduser()
        if not out.is_absolute():
            out = (repo_root / out).resolve()
        append_ts = config.get("append_timestamp", "true")
        if is_truthy(append_ts):
            return (out / f"{change}-{timestamp}").resolve()
        return out

    return (DEFAULT_OUTPUT_ROOT / f"{change}-{timestamp}").resolve()


def validate_change_name(name: str) -> None:
    if not name or ".." in name or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise SystemExit(f"ERROR: Invalid change name: {name!r}")


def change_dir_for(repo: Path, name: str) -> Path:
    active = repo / "openspec" / "changes" / name
    if active.is_dir():
        return active
    archived = repo / "openspec" / "changes" / "archive" / name
    if archived.is_dir():
        return archived
    return active


def parse_yaml_field(path: Path, field: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{field}:"):
            val = line.split(":", 1)[1].strip().strip("\"'")
            if val.startswith("#"):
                return ""
            return val
    return ""


def resolve_change(repo: Path, explicit: Optional[str]) -> str:
    if explicit:
        validate_change_name(explicit)
        return explicit

    active = []
    changes_root = repo / "openspec" / "changes"
    if changes_root.is_dir():
        for p in sorted(changes_root.iterdir()):
            if not p.is_dir() or p.name == "archive":
                continue
            if (p / ".comet.yaml").is_file():
                active.append(p.name)

    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        print("ERROR: Multiple active Comet changes. Specify change name:", file=sys.stderr)
        for n in active:
            print(f"  - {n}", file=sys.stderr)
        raise SystemExit(2)

    try:
        out = subprocess.run(
            ["openspec", "list", "--json"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(out.stdout)
        changes = [c for c in data.get("changes", []) if c.get("status") != "complete"]
        if not changes:
            changes = data.get("changes", [])
        if changes:
            changes.sort(key=lambda c: c.get("lastModified", ""), reverse=True)
            print(
                f"NOTE: No .comet.yaml; using latest OpenSpec change: {changes[0]['name']}",
                file=sys.stderr,
            )
            return changes[0]["name"]
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        pass

    raise SystemExit("ERROR: No active Comet change found. Create one with /comet-open first.")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def split_md_sections(text: str, level: int = 2) -> dict[str, str]:
    prefix = "#" * level + " "
    sections: dict[str, str] = {}
    current_key = "_intro"
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith(prefix) and not line.startswith("#" * (level + 1)):
            sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[len(prefix) :].strip()
            current_lines = []
        else:
            current_lines.append(line)
    sections[current_key] = "\n".join(current_lines).strip()
    if "_intro" in sections and not sections["_intro"]:
        del sections["_intro"]
    return sections


def split_md_subsections(text: str) -> dict[str, str]:
    return split_md_sections(text, level=3)


def parse_md_table(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if "|" not in line or re.match(r"^\|[\s\-:|]+\|$", line):
            continue
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def parse_bullets(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^[-*]\s+(.+)$", line.strip())
        if m:
            items.append(re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1).strip()))
    return items


def extract_codeblock(text: str, index: int = 0) -> str:
    blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return ""
    idx = min(index, len(blocks) - 1)
    return blocks[idx].strip()


def first_paragraph(text: str) -> str:
    for block in re.split(r"\n\s*\n", text.strip()):
        block = block.strip()
        if block and not block.startswith("|") and not block.startswith("```"):
            return re.sub(r"\*\*(.+?)\*\*", r"\1", block.replace("\n", " "))
    return ""


def extract_endpoint(text: str) -> str:
    m = re.search(r"`((?:GET|POST|PUT|DELETE|PATCH)\s+[^`]+)`", text)
    if m:
        return m.group(1).split(None, 1)[-1]
    m = re.search(r"`(/[^`]+)`", text)
    return m.group(1) if m else ""


def extract_metrics(text: str) -> list[tuple[str, str]]:
    """Parse key-value metric table (指标|值) from design/proposal."""
    for section_text in split_md_subsections(text).values():
        rows = parse_md_table(section_text)
        if not rows:
            continue
        header = [c.lower() for c in rows[0]]
        if len(header) >= 2 and "指标" in header[0] and "值" in header[1]:
            return [(r[0], r[1]) for r in rows[1:] if len(r) >= 2]
    rows = parse_md_table(text)
    if rows and len(rows[0]) >= 2 and "指标" in rows[0][0]:
        return [(r[0], r[1]) for r in rows[1:] if len(r) >= 2]
    return []


def extract_goal_target(text: str) -> str:
    m = re.search(r"P95[^≤]*≤\s*(\d+ms)", text)
    if m:
        return f"<{m.group(1)}"
    m = re.search(r"目标 P99[^<\d]*<?(\d+ms)", text)
    if m:
        return f"<{m.group(1)}"
    m = re.search(r"降低\s*≥?\s*(\d+%)", text)
    if m:
        return f"P95 -{m.group(1)}"
    return ""


def find_section_by_keyword(sections: dict[str, str], keywords: tuple[str, ...]) -> str:
    for key, body in sections.items():
        if any(kw in key for kw in keywords):
            return body
    return ""


def parse_tasks(tasks_path: Path) -> list[TaskItem]:
    if not tasks_path.is_file():
        return []

    items: list[TaskItem] = []
    section = "未分组"
    for raw in read_text(tasks_path).splitlines():
        line = raw.strip()
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = re.match(r"^- \[([ xX])\] (.+)$", line)
        if not m:
            continue
        done = m.group(1).lower() == "x"
        text = m.group(2).strip()
        task_id = ""
        id_m = re.match(r"^(\d+(?:\.\d+)*)\s+", text)
        if id_m:
            task_id = id_m.group(1)
        items.append(TaskItem(section=section, id=task_id, text=text, done=done))
    return items


def parse_decisions_table(design_text: str) -> list[tuple[str, str, str]]:
    """Extract decision rows: (topic, chosen, alternative)."""
    decisions_sec = split_md_sections(design_text).get("Decisions", "")
    if not decisions_sec:
        return []

    rows: list[tuple[str, str, str]] = []
    for title, body in split_md_subsections(decisions_sec).items():
        chosen = first_paragraph(body)
        alt = ""
        alt_m = re.search(r"\*\*备选\*\*[：:]\s*(.+?)(?:\n\n|\Z)", body, re.DOTALL)
        if alt_m:
            alt = alt_m.group(1).strip().split("\n")[0]
        topic = re.sub(r"^\d+\.\s*", "", title)
        rows.append((topic, chosen[:120] + ("…" if len(chosen) > 120 else ""), alt[:80]))
    return rows


def parse_design_highlights(design_text: str) -> list[tuple[str, str]]:
    """Return (title, code_or_text) for implementation highlights."""
    highlights: list[tuple[str, str]] = []
    decisions = split_md_sections(design_text).get("Decisions", "")
    for title, body in split_md_subsections(decisions).items():
        code = extract_codeblock(body)
        snippet = code if code else first_paragraph(body)[:200]
        if snippet:
            highlights.append((title, snippet))
    if highlights:
        return highlights[:4]

    for title, body in split_md_subsections(design_text).items():
        if any(k in title for k in ("调用链", "根因", "Goals")):
            continue
        code = extract_codeblock(body)
        if code:
            highlights.append((title, code))
    return highlights[:4]


def parse_spec_requirements(spec_path: Path) -> list[SpecRequirement]:
    text = read_text(spec_path)
    if not text:
        return []

    reqs: list[SpecRequirement] = []
    current_name = ""
    current_scenarios: list[str] = []

    for line in text.splitlines():
        if line.startswith("### Requirement:"):
            if current_name:
                reqs.append(SpecRequirement(current_name, current_scenarios))
            current_name = line.split(":", 1)[1].strip()
            current_scenarios = []
        elif line.startswith("#### Scenario:"):
            current_scenarios.append(line.split(":", 1)[1].strip())
    if current_name:
        reqs.append(SpecRequirement(current_name, current_scenarios))
    return reqs


def match_task_for_hotspot(hypothesis: str, tasks: list[TaskItem]) -> str:
    keywords = re.findall(r"[\u4e00-\u9fffA-Za-z_]+", hypothesis)
    best_id = ""
    best_score = 0
    for t in tasks:
        score = sum(1 for kw in keywords if len(kw) > 1 and kw.lower() in t.text.lower())
        if score > best_score:
            best_score = score
            best_id = t.id
    return best_id or "—"


def git_diff_stat(repo: Path, base_ref: str) -> str:
    if not base_ref:
        return ""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", base_ref],
            capture_output=True,
            check=True,
        )
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "--stat", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip() or "(no diff)"
    except subprocess.CalledProcessError:
        return "_(base_ref 不可解析)_"


def glob_first(repo: Path, patterns: list[str]) -> Optional[Path]:
    for pat in patterns:
        for p in sorted(repo.glob(pat)):
            if p.is_file():
                return p
    return None


def pct(done: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{done * 100 // total}%"


def phase_index(phase: str) -> int:
    try:
        return COMET_PHASES.index(phase.lower())
    except ValueError:
        return 0


def openspec_artifact_count(status: Optional[dict]) -> tuple[int, int]:
    if not status:
        return 0, 4
    arts = status.get("artifacts", [])
    done = sum(1 for a in arts if a.get("status") == "done")
    return done, len(arts) or 4


def load_context(repo: Path, change: str) -> ReportContext:
    change_dir = change_dir_for(repo, change)
    comet_yaml = change_dir / ".comet.yaml"
    archived = "archive" in change_dir.parts

    proposal_text = read_text(change_dir / "proposal.md")
    ctx = ReportContext(
        change=change,
        repo_root=repo,
        change_dir=change_dir,
        archived=archived,
        workflow=parse_yaml_field(comet_yaml, "workflow"),
        phase=parse_yaml_field(comet_yaml, "phase"),
        design_doc=parse_yaml_field(comet_yaml, "design_doc"),
        plan=parse_yaml_field(comet_yaml, "plan"),
        verify_report=parse_yaml_field(comet_yaml, "verification_report"),
        verify_result=parse_yaml_field(comet_yaml, "verify_result") or "pending",
        verify_mode=parse_yaml_field(comet_yaml, "verify_mode"),
        handoff_hash=parse_yaml_field(comet_yaml, "handoff_hash"),
        base_ref=parse_yaml_field(comet_yaml, "base_ref"),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        endpoint=extract_endpoint(proposal_text),
        git_diff=git_diff_stat(repo, parse_yaml_field(comet_yaml, "base_ref")),
    )

    ctx.tasks = parse_tasks(change_dir / "tasks.md")
    ctx.task_total = len(ctx.tasks)
    ctx.task_done = sum(1 for t in ctx.tasks if t.done)

    try:
        out = subprocess.run(
            ["openspec", "status", "--change", change, "--json"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        ctx.openspec_status = json.loads(out.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        ctx.openspec_status = None

    return ctx


def tag_html(label: str, css: str) -> str:
    return f'<span class="tag {css}">{esc(label)}</span>'


def metrics_html(metrics: list[tuple[str, str]], goal: str = "") -> str:
    cards = []
    for lbl, val in metrics:
        cards.append(
            f'<div class="metric-card"><div class="val">{esc(val)}</div>'
            f'<div class="lbl">{esc(lbl)}</div></div>'
        )
    if goal:
        cards.append(
            f'<div class="metric-card"><div class="val">{esc(goal)}</div>'
            f'<div class="lbl">优化目标</div></div>'
        )
    if not cards:
        return ""
    return f'<div class="metrics">{"".join(cards)}</div>'


def table_html(headers: list[str], rows: list[list[str]], raw_cells: Optional[list[list[str]]] = None) -> str:
    parts = ["<table>", "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"]
    for i, row in enumerate(rows):
        cells = raw_cells[i] if raw_cells and i < len(raw_cells) else None
        if cells:
            parts.append("<tr>" + "".join(cells) + "</tr>")
        else:
            parts.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "\n".join(parts)


def progress_bar_html(phase: str) -> str:
    idx = phase_index(phase)
    steps = []
    labels = ["Open", "Design", "Build", "Verify", "Archive"]
    for i, label in enumerate(labels):
        if i < idx:
            cls = "step-done"
            text = f"{label} ✓"
        elif i == idx:
            cls = "step-active"
            text = label
        else:
            cls = "step-pending"
            text = label
        steps.append(f'<div class="progress-step {cls}">{text}</div>')
    return f'<div class="progress-bar">{"".join(steps)}</div>'


def build_structured_data(ctx: ReportContext) -> dict:
    cd = ctx.change_dir
    proposal = read_text(cd / "proposal.md")
    design = read_text(cd / "design.md")
    proposal_sec = split_md_sections(proposal)
    design_sec = split_md_sections(design)
    design_sub = split_md_subsections(design)

    metrics = extract_metrics(design) or extract_metrics(proposal)
    goal = extract_goal_target(design) or extract_goal_target(proposal)

    before_chain = find_section_by_keyword(design_sub, ("当前调用链", "优化前", "调用链"))
    after_chain = find_section_by_keyword(design_sub, ("目标", "优化后", "数据流"))
    if not after_chain:
        after_chain = extract_codeblock(split_md_sections(design).get("Decisions", ""), 0)

    if not after_chain:
        decisions = parse_decisions_table(design)
        if decisions:
            after_chain = strip_md(decisions[0][1])[:400]

    hotspot_rows = parse_md_table(find_section_by_keyword(design_sub, ("根因", "热点", "ROI")))
    roi_rows: list[list[str]] = []
    if hotspot_rows and len(hotspot_rows[0]) >= 2:
        for i, row in enumerate(hotspot_rows[1:], 1):
            if len(row) < 2:
                continue
            hypothesis = strip_md(row[1] if row[0].startswith("H") else row[0])
            plan = strip_md(row[2] if len(row) > 2 else row[1])[:60]
            task_id = match_task_for_hotspot(hypothesis, ctx.tasks)
            benefit = ROI_BENEFIT[min(i - 1, len(ROI_BENEFIT) - 1)]
            roi_rows.append([str(i), hypothesis, plan, task_id, benefit])

    specs: list[SpecRequirement] = []
    for spec_path in sorted(cd.glob("specs/**/spec.md")):
        specs.extend(parse_spec_requirements(spec_path))

    design_doc_path = None
    if ctx.design_doc and ctx.design_doc not in ("null", ""):
        p = ctx.repo_root / ctx.design_doc
        if p.is_file():
            design_doc_path = p
    if design_doc_path is None:
        design_doc_path = glob_first(
            ctx.repo_root,
            [
                f"docs/superpowers/specs/*{ctx.change}*design.md",
                f"docs/superpowers/specs/*{ctx.change.replace('-', '_')}*design.md",
            ],
        )

    os_done, os_total = openspec_artifact_count(ctx.openspec_status)
    hash_short = ctx.handoff_hash[:8] + "…" if len(ctx.handoff_hash) > 8 else ctx.handoff_hash

    return {
        "proposal": proposal,
        "design": design,
        "proposal_sec": proposal_sec,
        "design_sec": design_sec,
        "background": first_paragraph(proposal_sec.get("Why", "")) or first_paragraph(design_sec.get("Context", "")),
        "metrics": metrics,
        "goal": goal,
        "before_chain": extract_codeblock(before_chain) or before_chain.strip()[:800],
        "after_chain": after_chain.strip()[:800] if after_chain else "",
        "roi_rows": roi_rows,
        "decisions": parse_decisions_table(design),
        "open_questions": parse_bullets(design_sec.get("Open Questions", "")),
        "migration_refs": parse_bullets(design_sec.get("Migration Plan", ""))[:1],
        "highlights": parse_design_highlights(design),
        "specs": specs,
        "design_doc_path": design_doc_path,
        "os_done": os_done,
        "os_total": os_total,
        "hash_short": hash_short,
    }


def build_html(ctx: ReportContext, data: dict) -> str:
    ps = data["proposal_sec"]
    endpoint_meta = f' &nbsp;|&nbsp; 接口：<code>{esc(ctx.endpoint)}</code>' if ctx.endpoint else ""
    date_short = ctx.generated_at.split()[0]

    # §1
    s1_parts = [
        '<section id="s1"><h2>§1 分析结果</h2>',
        "<h3>问题背景</h3>",
        f"<p>{esc(strip_md(data['background']))}</p>",
    ]
    if data["metrics"]:
        s1_parts.append(metrics_html(data["metrics"], data["goal"]))
    if data["before_chain"]:
        s1_parts.extend(["<h3>优化前调用链</h3>", f"<pre>{esc(data['before_chain'])}</pre>"])
    if data["after_chain"]:
        s1_parts.extend(["<h3>目标优化后数据流</h3>", f"<pre>{esc(data['after_chain'])}</pre>"])
    if data["roi_rows"]:
        s1_parts.extend(
            [
                "<h3>性能热点 ROI</h3>",
                table_html(["#", "热点", "方案", "Task", "收益"], data["roi_rows"]),
            ]
        )
    s1_parts.append("</section>")

    # §2
    s2_parts = [
        '<section id="s2"><h2>§2 头脑风暴总结</h2>',
        f"<p><strong>来源：</strong>design-context.md（hash: {esc(data['hash_short'] or '—')}）+ Design 阶段讨论</p>",
    ]
    if data["decisions"]:
        s2_parts.append(
            table_html(
                ["决策", "选用", "备选"],
                data["decisions"],
            )
        )
    refs = data.get("migration_refs") or []
    if refs:
        s2_parts.append(f"<p><strong>参考：</strong><code>{esc(strip_md(refs[0]))}</code></p>")
    if data["open_questions"]:
        s2_parts.append(
            f"<p><strong>Open Question：</strong>{esc(data['open_questions'][0])}</p>"
        )
    s2_parts.append("</section>")

    # §3
    s3_parts = ['<section id="s3"><h2>§3 Proposal</h2>']
    if ps.get("Why"):
        s3_parts.extend(["<h3>Why</h3>", f"<p>{esc(first_paragraph(ps['Why']))}</p>"])
    bullets = parse_bullets(ps.get("What Changes", ""))
    if bullets:
        s3_parts.append("<h3>What Changes</h3><ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
    caps = parse_bullets(ps.get("Capabilities", ""))
    if not caps:
        caps_sec = split_md_subsections(ps.get("Capabilities", ""))
        new_caps = parse_bullets(caps_sec.get("New Capabilities", ""))
        caps = new_caps
    if caps:
        cap_name = strip_md(caps[0].split(":")[0])
        s3_parts.append(f"<h3>Capabilities</h3><p>新增 <code>{esc(cap_name)}</code>；无 Modified Capabilities</p>")
    impact = first_paragraph(ps.get("Impact", "")) or "；".join(parse_bullets(ps.get("Impact", ""))[:3])
    if impact:
        s3_parts.append(f"<h3>Impact</h3><p>{esc(impact)}</p>")
    s3_parts.append("</section>")

    # §4
    design_doc = data["design_doc_path"]
    design_doc_status = tag_html("已生成", "tag-done") if design_doc else tag_html("未生成", "tag-wait")
    s4_parts = ['<section id="s4"><h2>§4 Design</h2>']
    phase_note = "深度设计已产出" if data["design"] else "design.md 待补充"
    doc_note = "Design Doc 待确认" if not design_doc else "Design Doc 已关联"
    s4_parts.append(
        f'<div class="notice notice-info">Design 入口检查 PASS；{phase_note}；<strong>{doc_note}</strong></div>'
    )
    if data["highlights"]:
        s4_parts.append("<h3>实现要点</h3>")
        for title, snippet in data["highlights"]:
            s4_parts.append(f"<h4>{esc(title)}</h4><pre>{esc(snippet)}</pre>")
    doc_path = str(design_doc) if design_doc else f"docs/superpowers/specs/*{ctx.change}*design.md"
    s4_parts.append(
        f"<p><strong>Design Doc：</strong>{design_doc_status} → <code>{esc(doc_path)}</code></p>"
    )
    s4_parts.append("</section>")

    # §5
    diff_note = ctx.git_diff or "(no diff)"
    s5_parts = [
        '<section id="s5"><h2>§5 Tasks</h2>',
        f'<div class="notice"><code>git diff {esc(ctx.base_ref[:8] if ctx.base_ref else "?")}...HEAD</code> {esc(diff_note)} — <strong>{ctx.task_done}/{ctx.task_total} 完成</strong></div>',
    ]
    if ctx.tasks:
        task_rows = []
        task_raw = []
        for t in ctx.tasks:
            label, css = t.status_label()
            task_rows.append([t.id or "—", t.short_title, t.detail[:80], label, t.verify_hint])
            task_raw.append(
                [
                    f"<td>{esc(t.id or '—')}</td>",
                    f"<td>{esc(t.short_title)}</td>",
                    f"<td>{esc(t.detail[:80])}</td>",
                    f"<td>{tag_html(label, css)}</td>",
                    f"<td>{esc(t.verify_hint)}</td>",
                ]
            )
        s5_parts.append(
            table_html(["ID", "任务", "具体做什么", "状态", "验证"], task_rows, task_raw)
        )
    else:
        s5_parts.append("<p class='muted'>tasks.md 无任务项</p>")
    s5_parts.append("</section>")

    # §6
    verify_mode = ctx.verify_mode if ctx.verify_mode not in ("", "null") else "未设置"
    verify_tag = tag_html(ctx.verify_result, "tag-done" if ctx.verify_result == "pass" else "tag-pending")
    report_tag = tag_html("已生成", "tag-done") if ctx.verify_report not in ("", "null") else tag_html("未生成", "tag-pending")
    s6_parts = [
        '<section id="s6"><h2>§6 Verify</h2>',
        f"<p>verify_mode: {esc(verify_mode)} &nbsp;|&nbsp; verify_result: {verify_tag} &nbsp;|&nbsp; report: {report_tag}</p>",
    ]
    if data["specs"]:
        spec_rows = []
        spec_raw = []
        for req in data["specs"]:
            scenarios = " / ".join(req.scenarios[:2]) if req.scenarios else "—"
            status = tag_html("已验证", "tag-done") if ctx.verify_result == "pass" else tag_html("未验证", "tag-pending")
            short_name = req.name.split()[0] if req.name else req.name
            if len(req.name) <= 40:
                short_name = req.name
            spec_rows.append([short_name, scenarios, "未验证"])
            spec_raw.append([f"<td>{esc(short_name)}</td>", f"<td>{esc(scenarios)}</td>", f"<td>{status}</td>"])
        s6_parts.append(table_html(["Spec Requirement", "Scenarios", "状态"], spec_rows, spec_raw))
    s6_parts.append("</section>")

    # §7
    s7_parts = [
        '<section id="s7"><h2>§7 Archive</h2>',
        f"<p>archived: <strong>{str(ctx.archived).lower()}</strong> &nbsp;|&nbsp; 目录: <code>{esc(str(ctx.change_dir.relative_to(ctx.repo_root)))}</code></p>",
        table_html(
            ["产物", "状态"],
            [
                [f"OpenSpec {data['os_done']}/{data['os_total']}", "完成" if data["os_done"] == data["os_total"] else "进行中"],
                ["handoff", data["hash_short"] or "—"],
                ["Design Doc / Plan / Verify", "已生成" if design_doc else "未生成"],
                ["主 spec 同步", "已同步" if ctx.archived else "未同步"],
            ],
            [
                [f"<td>OpenSpec {data['os_done']}/{data['os_total']}</td>", tag_html("完成", "tag-done") if data["os_done"] == data["os_total"] else tag_html("进行中", "tag-active")],
                [f"<td>handoff</td>", tag_html(data["hash_short"] or "—", "tag-done") if data["hash_short"] else tag_html("—", "tag-pending")],
                ["<td>Design Doc / Plan / Verify</td>", design_doc_status if design_doc else tag_html("未生成", "tag-pending")],
                ["<td>主 spec 同步</td>", tag_html("已同步", "tag-done") if ctx.archived else tag_html("未同步", "tag-pending")],
            ],
        ),
        "<ol>",
        "<li>确认 Design → 写 Design Doc</li>",
        "<li>/comet-build 实施</li>",
        "<li>灰度 + APM</li>",
        "<li>/comet-archive</li>",
        "</ol>",
        "</section>",
    ]

    # §8
    idx = phase_index(ctx.phase)
    phase_rows = []
    phase_raw = []
    stage_info = [
        ("Open", 0, "proposal / design / tasks / spec"),
        ("Design", 1, "handoff ✓；Design Doc 待确认" if not design_doc else "handoff ✓；Design Doc 已关联"),
        ("Build", 2, f"{ctx.task_done}/{ctx.task_total} tasks；{diff_note[:40]}"),
        ("Verify", 3, ctx.verify_result),
        ("Archive", 4, "—"),
    ]
    for name, pidx, artifact in stage_info:
        if pidx < idx:
            st, css = "完成", "tag-done"
        elif pidx == idx:
            st, css = "进行中", "tag-active"
        else:
            st, css = "未开始", "tag-pending"
        phase_rows.append([name, st, artifact])
        phase_raw.append([f"<td>{name}</td>", tag_html(st, css), f"<td>{esc(artifact)}</td>"])

    yaml_summary = (
        f"phase: {ctx.phase or '—'} | workflow: {ctx.workflow or '—'} | base_ref: {ctx.base_ref[:8] if ctx.base_ref else '—'}\n"
        f"handoff_hash: {ctx.handoff_hash or '—'}\n"
        f"design_doc: {ctx.design_doc or 'null'} | plan: {ctx.plan or 'null'} | verify_result: {ctx.verify_result}"
    )

    s8_parts = [
        '<section id="s8"><h2>§8 Comet 工作流总览</h2>',
        progress_bar_html(ctx.phase),
        table_html(["阶段", "状态", "产物"], phase_rows, phase_raw),
        f"<pre>{esc(yaml_summary)}</pre>",
        "</section>",
    ]

    nav = """
<nav>
<h2>Comet 报告导航</h2>
<a href="#s1">§1 分析结果</a>
<a href="#s2">§2 头脑风暴总结</a>
<a href="#s3">§3 Proposal</a>
<a href="#s4">§4 Design</a>
<a href="#s5">§5 Tasks</a>
<a href="#s6">§6 Verify</a>
<a href="#s7">§7 Archive</a>
<a href="#s8">§8 工作流总览</a>
</nav>"""

    css = """
:root{--bg:#f8f9fa;--sidebar:#1a1d23;--sidebar-text:#c9cdd3;--sidebar-active:#4f8cff;--card:#fff;--border:#e2e5ea;--text:#1a1d23;--muted:#6b7280;--green:#16a34a;--yellow:#ca8a04;--gray:#9ca3af;--blue:#2563eb}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.65}
.layout{display:flex;min-height:100vh}
nav{position:fixed;top:0;left:0;width:240px;height:100vh;background:var(--sidebar);padding:24px 16px;overflow-y:auto;z-index:100}
nav h2{color:#fff;font-size:14px;margin-bottom:16px}
nav a{display:block;color:var(--sidebar-text);text-decoration:none;font-size:13px;padding:6px 10px;border-radius:6px;margin-bottom:2px}
nav a:hover{background:rgba(79,140,255,.15);color:var(--sidebar-active)}
main{margin-left:240px;flex:1;padding:32px 40px 80px;max-width:960px}
h1{font-size:28px;margin-bottom:8px}
.meta{color:var(--muted);font-size:14px;margin-bottom:32px}
section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:28px;margin-bottom:24px}
section h2{font-size:20px;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid var(--border)}
section h3{font-size:16px;margin:20px 0 10px;color:var(--blue)}
section h4{font-size:14px;margin:14px 0 6px}
p,li{font-size:14px;margin-bottom:8px}
ul,ol{padding-left:20px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:12px 0}
th,td{border:1px solid var(--border);padding:8px 12px;text-align:left;vertical-align:top}
th{background:#f1f3f5;font-weight:600}
pre{background:#1e293b;color:#e2e8f0;padding:14px 16px;border-radius:8px;overflow-x:auto;font-size:12px;line-height:1.5;margin:12px 0;white-space:pre-wrap}
.tag{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}
.tag-done{background:#dcfce7;color:var(--green)}
.tag-pending{background:#f3f4f6;color:var(--gray)}
.tag-wait{background:#fef9c3;color:var(--yellow)}
.tag-active{background:#dbeafe;color:var(--blue)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:16px 0}
.metric-card{background:#f1f3f5;border-radius:8px;padding:14px;text-align:center}
.metric-card .val{font-size:22px;font-weight:700;color:var(--blue)}
.metric-card .lbl{font-size:12px;color:var(--muted);margin-top:4px}
.notice{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px 16px;font-size:13px;margin:12px 0}
.notice-info{background:#eff6ff;border-color:#bfdbfe}
.muted{color:var(--muted);font-style:italic}
@media print{nav{display:none}main{margin-left:0;max-width:100%}section{break-inside:avoid}}
"""

    body = "\n".join(s1_parts + s2_parts + s3_parts + s4_parts + s5_parts + s6_parts + s7_parts + s8_parts)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Comet 报告 — {esc(ctx.change)}</title>
<style>{css}</style>
</head>
<body>
<div class="layout">
{nav}
<main>
<h1>Comet 报告：{esc(ctx.change)}</h1>
<p class="meta">生成时间：{esc(date_short)} &nbsp;|&nbsp; Change：<code>{esc(ctx.change)}</code>{endpoint_meta}</p>
{body}
</main>
</div>
</body>
</html>"""


def build_markdown(ctx: ReportContext, data: dict) -> str:
    ps = data["proposal_sec"]
    lines = [
        f"# Comet 报告 — {ctx.change}",
        "",
        f"> 生成时间：{ctx.generated_at} | 阶段：**{ctx.phase}** | 任务：**{ctx.task_done}/{ctx.task_total}**",
        "",
        "---",
        "",
        "## §1 分析结果",
        "",
        "### 问题背景",
        "",
        data["background"] or "_（无）_",
        "",
    ]

    if data["metrics"]:
        lines.extend(["### 关键指标", "", "| 指标 | 值 |", "|------|-----|"])
        for lbl, val in data["metrics"]:
            lines.append(f"| {lbl} | {val} |")
        if data["goal"]:
            lines.append(f"| 优化目标 | {data['goal']} |")
        lines.append("")

    if data["before_chain"]:
        lines.extend(["### 优化前调用链", "", "```", data["before_chain"], "```", ""])
    if data["after_chain"]:
        lines.extend(["### 目标优化后数据流", "", "```", data["after_chain"], "```", ""])
    if data["roi_rows"]:
        lines.extend(["### 性能热点 ROI", "", "| # | 热点 | 方案 | Task | 收益 |", "|---|------|------|------|------|"])
        for row in data["roi_rows"]:
            lines.append("| " + " | ".join(strip_md(c) for c in row) + " |")
        lines.append("")

    lines.extend(["---", "", "## §2 头脑风暴总结", ""])
    if data["decisions"]:
        lines.extend(["| 决策 | 选用 | 备选 |", "|------|------|------|"])
        for d in data["decisions"]:
            lines.append(f"| {d[0]} | {d[1]} | {d[2] or '—'} |")
        lines.append("")
    if data["open_questions"]:
        lines.append(f"**Open Question：** {data['open_questions'][0]}")
        lines.append("")

    lines.extend(["---", "", "## §3 Proposal", ""])
    if ps.get("Why"):
        lines.extend(["### Why", "", first_paragraph(ps["Why"]), ""])
    bullets = parse_bullets(ps.get("What Changes", ""))
    if bullets:
        lines.append("### What Changes")
        lines.extend(f"- {b}" for b in bullets)
        lines.append("")

    lines.extend(["---", "", "## §4 Design", ""])
    for title, snippet in data["highlights"]:
        lines.extend([f"### {title}", "", "```", snippet[:500], "```", ""])

    lines.extend(["---", "", "## §5 Tasks", ""])
    lines.append(f"`git diff {ctx.base_ref[:8] if ctx.base_ref else '?'}...HEAD` → {ctx.git_diff or '(no diff)'} — **{ctx.task_done}/{ctx.task_total} 完成**")
    lines.append("")
    if ctx.tasks:
        lines.extend(["| ID | 任务 | 具体做什么 | 状态 | 验证 |", "|----|------|------------|------|------|"])
        for t in ctx.tasks:
            label, _ = t.status_label()
            icon = "✅" if t.done else ("⏳" if label == "待验证" else "⬜")
            lines.append(f"| {t.id} | {t.short_title} | {t.detail[:60]} | {icon} {label} | {t.verify_hint} |")
        lines.append("")

    lines.extend(["---", "", "## §6 Verify", ""])
    lines.append(f"verify_result: **{ctx.verify_result}**")
    lines.append("")
    if data["specs"]:
        lines.extend(["| Spec Requirement | Scenarios | 状态 |", "|---|---|---|"])
        for req in data["specs"]:
            scenarios = " / ".join(req.scenarios[:2])
            st = "✅" if ctx.verify_result == "pass" else "⬜ 未验证"
            lines.append(f"| {req.name} | {scenarios} | {st} |")
        lines.append("")

    lines.extend(["---", "", "## §8 Comet 工作流总览", ""])
    idx = phase_index(ctx.phase)
    for i, ph in enumerate(["Open", "Design", "Build", "Verify", "Archive"]):
        mark = "✓" if i < idx else ("→" if i == idx else " ")
        lines.append(f"- [{mark}] {ph}")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Comet Markdown + HTML reports")
    parser.add_argument("change", nargs="?", help="OpenSpec change name")
    parser.add_argument(
        "--output-dir",
        "-o",
        help="Exact output directory (overrides COMET_REPORT_DIR and .comet-report.yaml)",
    )
    args = parser.parse_args()

    repo = find_repo_root(Path.cwd())
    change = resolve_change(repo, args.change)
    ctx = load_context(repo, change)
    data = build_structured_data(ctx)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = resolve_output_dir(repo, change, ts, args.output_dir)

    out_dir.mkdir(parents=True, exist_ok=True)

    md_path = out_dir / "report.md"
    html_path = out_dir / "report.html"
    meta_path = out_dir / "manifest.json"

    md_content = build_markdown(ctx, data)
    html_content = build_html(ctx, data)

    md_path.write_text(md_content, encoding="utf-8")
    html_path.write_text(html_content, encoding="utf-8")

    manifest = {
        "change": change,
        "generated_at": ctx.generated_at,
        "output_dir": str(out_dir),
        "markdown": str(md_path),
        "html": str(html_path),
        "task_done": ctx.task_done,
        "task_total": ctx.task_total,
        "phase": ctx.phase,
        "format_version": "2",
    }
    meta_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Comet report directory: {out_dir}", file=sys.stderr)
    print(str(out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
