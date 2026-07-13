---
name: paper-survey
description: 主题调研 — 基于已有文献库创建调研、AI 分类筛选论文、导出 CSV 结果。触发词: "文献调研", "paper survey", "论文筛选", "拉论文", "/paper-survey"。当用户想要做学术文献调研、筛选特定主题的论文时使用。前提: 文献库已通过 paper-database 技能初始化。
version: 0.1.0
---

## 职责

你是翻译层。所有实际工作由 `python -m paper_database survey` 的 CLI 命令完成。
你只负责理解用户意图 → 执行对应命令 → 汇报结果。

## 前提条件

- 文献库已通过 `paper-database` 技能初始化（有论文数据）
- 环境变量 `PAPER_DATABASE_HOME` 指向项目根目录
- 分类器工具已配置（`config/classifier.yaml`）

## 命令速查

| 用户意图 | 执行命令 |
|---------|---------|
| 创建调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling` |
| 列出所有调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey list` |
| 看调研进度 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey stats --survey-id <id>` |
| 先测试几篇 prompt | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --dry-run --limit 3` |
| 开始分类 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>` |
| 分类 50 篇后暂停 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --limit 50` |
| 从第 100 篇续传 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --start 100` |
| 终端预览结果 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id> --relevant-only` |
| 导出 CSV | `cd $PAPER_DATABASE_HOME && python -m paper_database survey export --survey-id <id> [--relevant-only]` |
| 删除调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey delete --survey-id <id>` |

## 典型对话流程

### 场景1: 新建完整调研
用户: "帮我做调度相关的文献调研"

1. **先检查文献库**: `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats`
   - 如果论文总数为 0: 告知用户 "文献库为空。请先使用 paper-database 技能初始化文献库并拉取论文。" 就此停止。
2. **创建调研**: `cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling --name "调度调研YYYYMMDD"`
3. **建议 dry-run**: `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --dry-run --limit 3`
   - 让用户确认 prompt 是否合理
   - 用户可以此时调整 `config/classifier.yaml` 中 prompt_template 或 `config/topics.yaml` 中 keywords
4. **正式分类**: `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>`
   - 分类中可随时 Ctrl+C 暂停，之后用 `--start N` 续传
5. **完成后**: `cd $PAPER_DATABASE_HOME && python -m paper_database survey export --survey-id <id> --relevant-only`
6. 也可以先 preview: `cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id> --relevant-only`

### 场景2: 只查某个会议的论文
用户: "我要看 ISCA 2024 有哪些调度论文"

1. `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue isca --year 2024`
2. `cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling --venue-filter isca --year-filter 2024-2024`
3. `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>`
4. `cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id> --relevant-only`

### 场景3: 用已有论文数据做新主题调研
用户: "论文已经拉过了，帮我做个功耗相关的调研"

1. 检查 `config/topics.yaml` 有没有该 topic
   - 如果没有: 告诉用户去 `config/topics.yaml` 添加新 topic（格式参考已有的 scheduling）
2. `cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic power --name "功耗调研"`
3. `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>`

### 场景4: 查看已有调研结果
用户: "看看上次调度调研的结果"

1. `cd $PAPER_DATABASE_HOME && python -m paper_database survey list`
2. `cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id> --relevant-only --limit 20`

## 重要提醒

- 分类是逐篇 subprocess 调 `claude` CLI，每篇约 1-2 秒
- 大批量分类建议在终端直接跑 `survey classify`（不经过 Skill 对话，避免 token 开销）
- `--start N` 用于断点续传，N 是从第几篇开始（从1开始）
- `dry-run` 不消耗 LLM 调用，用于检查 prompt
- 如果文献库没有数据，引导用户使用 `paper-database` 技能先建库
- 不同 topic 的配置在 `config/topics.yaml` 中定义，可以自行添加