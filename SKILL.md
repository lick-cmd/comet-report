---
name: comet-report
description: "生成 Comet 全流程 Markdown + HTML 报告。触发词：生成comet报告、comet-report、/comet-report。用户要汇总 OpenSpec/Superpowers 变更产物、查看任务完成情况、导出 Comet 报告时务必使用本技能。支持 .comet-report.yaml、COMET_REPORT_DIR、--output-dir 自定义输出路径。"
---

# Comet 报告（comet-report）

当用户输入 **`生成comet报告`**、**`comet-report`** 或 **`/comet-report`** 时执行本技能。

## 目标

1. 扫描当前 **Comet 变更**（OpenSpec change + `.comet.yaml`）
2. 从 proposal / design / tasks / specs **结构化提取**关键信息（非原文 dump）
3. 按配置写入报告目录，生成 **`report.md`**、**`report.html`**、**`manifest.json`**

## 输出路径（必须遵守优先级）

| 优先级 | 方式 | 行为 |
|--------|------|------|
| **1** | 用户指定路径 / `-o` | **精确目录**，不追加时间戳子目录 |
| **2** | `COMET_REPORT_DIR` 环境变量 | 根目录 + `<change>-<时间戳>/` |
| **3** | 仓库根 `.comet-report.yaml` | 见 `append_timestamp`（本仓库默认 `docs/comet-reports/`） |
| **4** | 默认 | `~/Documents/CometReport/<change>-<时间戳>/` |

**Agent 规则**：用户说「输出到 xxx」时，必须传 `-o xxx`，不要写死 `~/Documents/CometReport`。

## 步骤

### 1. 进入仓库根目录

目录需包含 `openspec/`。

### 2. 运行生成脚本（必须）

**推荐（包装脚本）：**

```bash
bash .claude/skills/comet-report/scripts/comet-report.sh [change-name]
bash .claude/skills/comet-report/scripts/comet-report.sh my-change -o ./docs/comet-reports/pinned
```

**或通过 comet-env：**

```bash
COMET_ENV="${COMET_ENV:-$(find . "$HOME"/.claude/skills "$HOME"/.cursor/skills -path '*/comet/scripts/comet-env.sh' -type f -print -quit 2>/dev/null)}"
. "$COMET_ENV"
bash "$COMET_REPORT" [change-name] [-o 输出目录]
```

**或直接 Python（等价）：**

```bash
python3 .claude/skills/comet-report/scripts/generate_report.py [change-name] [-o 输出目录]
```

- **stdout 最后一行**：报告目录绝对路径
- **退出码 2**：多个活跃 change，须让用户指定名称后重跑

### 3. 向用户汇报

说明：

- change 名、Comet 阶段、任务进度（已完成/总数）
- **实际使用的输出路径**（含 `report.md`、`report.html`、`manifest.json`）
- 缺失章节（无 handoff、无 design doc 等）

## 团队开箱配置

本仓库根目录已有 `.comet-report.yaml`：

```yaml
output_dir: docs/comet-reports
append_timestamp: true
```

他人 clone 后无需配置即可生成到 `docs/comet-reports/<change>-<时间戳>/`。详见 `.claude/skills/comet-report/README.md`。

## 报告结构（v2）

HTML 采用**左侧导航 + 卡片式章节**：

| 章节 | 说明 |
|------|------|
| **§1 分析结果** | 问题背景、指标卡片、优化前/后调用链、性能热点 ROI 表 |
| **§2 头脑风暴总结** | 决策对比表、Open Questions |
| **§3 Proposal** | Why / What Changes / Capabilities / Impact |
| **§4 Design** | 实现要点、Design Doc 状态 |
| **§5 Tasks** | git diff 提示 + 五列任务表 |
| **§6 Verify** | verify 状态 + Delta Spec Requirements |
| **§7 Archive** | 产物状态、后续步骤 |
| **§8 工作流总览** | 阶段进度条 + `.comet.yaml` 摘要 |

## 约束

- **只读**：不修改 change、不推进 Comet 阶段
- 任意阶段均可生成（open / design / build / verify / archive）
- 不要手写报告替代脚本输出

## 故障处理

| 现象 | 处理 |
|------|------|
| 无活跃 change | 提示 `/comet-open` |
| 多个活跃 change | 列出名称，让用户指定 |
| `openspec` 未安装 | 报告仍生成，OpenSpec 计数省略 |
| Python 不可用 | 报错并提示安装 Python 3 |
| `COMET_REPORT` 未设置 | 直接用 `comet-report.sh` 或 `python3 generate_report.py` |
