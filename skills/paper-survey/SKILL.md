---
name: paper-survey
description: 主题调研 — 基于已有文献库创建调研、AI 分类筛选论文(支持磋商投票)、导出 CSV/Markdown。触发词: "文献调研", "paper survey", "论文筛选", "/paper-survey"。前提: 文献库已通过 paper-database 技能初始化。
version: 0.4.0
---

## 核心规则

**你是命令生成器，不是执行器。** 秒级操作可直接执行，分钟级只生成命令。

| 操作 | 耗时 | 方式 |
|------|------|------|
| `survey list/stats/create/preview/export/export-md` | <2s | **可执行** |
| `survey classify --dry-run --limit 3` | <1s | **可执行** |
| `survey classify -d "title"` | ~2s | **可执行** |
| `survey classify` (>50 篇) | 分钟级 | **生成命令** |
| `survey classify --deliberate 3` | 3× 耗时 | **生成命令** |
| `survey translate` | 分钟级 | **生成命令** |

## 前提

- 文献库已有论文数据（用 `paper-database` 建库）
- `$PAPER_DATABASE_HOME` 指向项目根目录
- 分类器已配置 `config/llm.yaml`

## 命令速查

| 用户意图 | 命令 | 耗时 |
|---------|------|------|
| 创建调研 | `survey create -t scheduling [-n name] [--venue-filter isca,hpca] [--year-filter 2020-2024]` | <2s |
| 列出调研 | `survey list` | <1s |
| 查看进度 | `survey stats -s X` | <1s |
| dry-run 测试 | `survey classify -s X --dry-run --limit 3` | <1s |
| 正式分类 | `survey classify -s X` | 15–60min |
| 磋商投票 | `survey classify -s X --deliberate 3` | 更长 |
| 小批测试 | `survey classify -s X --limit 50` | ~2min |
| 调试单篇 | `survey classify -s X -d "标题关键词"` | ~2s |
| 终端预览 | `survey preview -s X` | <2s |
| 导出 CSV | `survey export -s X [-o 路径]` | <2s |
| 导出 Markdown | `survey export-md -s X [-o 目录]` | <2s |
| 翻译摘要 | `survey translate -s X [--limit 50]` | 分钟级 |
| 清空分类 | `survey reset -s X` | <1s |
| 删除调研 | `survey delete -s X` | <1s |

所有 `survey` 命令在 `cd $PAPER_DATABASE_HOME` 后执行，`--survey-id X` 可简写 `-s X`。

## 典型流程

### 新建调研

```bash
cd $PAPER_DATABASE_HOME

# 1. 创建
python -m paper_database survey create -t scheduling -n "调度调研"

# 2. dry-run 检查 prompt（可执行）
python -m paper_database survey classify -s X --dry-run --limit 3

# 3. 正式分类（生成命令给用户跑）
python -m paper_database survey classify -s X    # 预计 15–60min，中断可续传

# 4. 查看结果（可执行）
python -m paper_database survey preview -s X
python -m paper_database survey export -s X      # CSV
python -m paper_database survey export-md -s X   # Markdown
```

分类完成自动导出 CSV 到 `results/survey_X_<name>.csv`。`export-md` 按 venue 分文件到 `results/survey_X_md/`。

### 按 venue + 年份筛选

```bash
cd $PAPER_DATABASE_HOME
python -m paper_database survey create -t scheduling --venue-filter isca --year-filter 2024-2024 -n "ISCA2024"
python -m paper_database survey classify -s X
```

### 磋商投票分类

```bash
python -m paper_database survey classify -s X --deliberate 3
```

LLM 有随机性，`--deliberate N` 每篇并行 N 轮 → 投票聚合。策略: `majority`/`supermajority`/`consensus` (在 `config/llm.yaml` 配置)。

### 调试分类效果

```bash
# 单篇调试：看完整 prompt + 原始响应
python -m paper_database survey classify -s X -d "CGRA" --deliberate 3
```

### 断点续传

中断后直接重新运行相同命令，已分类自动跳过：
```bash
python -m paper_database survey classify -s X --limit 200   # 第一批
python -m paper_database survey classify -s X               # 继续跑完
```

### 翻译入选摘要

```bash
python -m paper_database survey translate -s X [--limit 50]
```

## 输出字段配置

所有字段由 `config/topics.yaml` 的 `output.columns` 驱动，无需改代码：

- `venue_*` / `paper_*` 前缀 → JOIN 自 venue/paper 表
- 无前缀 → 自动建为 `survey_result` 表列（列名 = prompt JSON key）
- `rank` 字段 → 定义值排序（Markdown 导出时使用）

新增字段：`prompt_template` JSON 加 key + `output.columns` 加行 + 重建 survey。代码全栈通用，不做字段假设。

## 重要提醒

- **禁止直接操作数据库** — 所有操作通过 CLI 完成
- 分类是并发调 LLM API，不是 subprocess 调 claude CLI
- 大批量分类建议终端直接跑（tmux），`--limit` 用于分批验证
- 先 `paper enrich --doi-only` 补摘要再创建 survey，分类质量更高
- 文献库无数据时引导用户用 `paper-database` 技能建库
