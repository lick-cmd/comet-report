# comet-report

从 OpenSpec + Comet 变更产物生成 **Markdown + HTML** 结构化报告（§1–§8）。

## 依赖

- Python 3
- 仓库根目录存在 `openspec/`
- 至少一个 Comet change（`openspec/changes/<name>/`）

## 快速开始

```bash
# 在仓库根目录执行
bash .claude/skills/comet-report/scripts/comet-report.sh

# 指定 change
bash .claude/skills/comet-report/scripts/comet-report.sh my-change-name

# 指定精确输出目录（最高优先级）
bash .claude/skills/comet-report/scripts/comet-report.sh my-change -o ./docs/comet-reports/pinned
```

本仓库已配置 `.comet-report.yaml`，默认输出到：

`docs/comet-reports/<change名>-<时间戳>/`

## 输出路径优先级

| 优先级 | 方式 | 结果目录 |
|--------|------|----------|
| 1 | `-o / --output-dir` | 精确目录，不追加时间戳 |
| 2 | `COMET_REPORT_DIR` 环境变量 | `<根目录>/<change>-<时间戳>/` |
| 3 | `.comet-report.yaml` 的 `output_dir` | 由 `append_timestamp` 控制 |
| 4 | 默认 | `~/Documents/CometReport/<change>-<时间戳>/` |

## 配置文件

仓库根目录 `.comet-report.yaml`（可参考 `.comet-report.yaml.example`）：

```yaml
output_dir: docs/comet-reports
append_timestamp: true
```

## 与 comet-env 集成

```bash
. .claude/skills/comet/scripts/comet-env.sh   # 或 .cursor/skills/comet/scripts/comet-env.sh
bash "$COMET_REPORT" [change-name] [-o 输出目录]
```

## Cursor / Agent

触发词：`生成comet报告`、`comet-report`、`/comet-report`

用户若指定路径，Agent 必须传 `-o` 或先改 `.comet-report.yaml`。

## 产物

每次生成目录内包含：

- `report.md`
- `report.html`（左侧导航 + 卡片章节）
- `manifest.json`
