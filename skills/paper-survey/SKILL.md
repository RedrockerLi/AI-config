---
name: paper-survey
description: 主题调研 — 基于已有文献库创建调研、AI 分类筛选论文(支持磋商投票)、导出 CSV 结果。触发词: "文献调研", "paper survey", "论文筛选", "拉论文", "/paper-survey"。当用户想要做学术文献调研、筛选特定主题的论文时使用。前提: 文献库已通过 paper-database 技能初始化。
version: 0.3.0
---

## 核心规则

**你是命令生成器，不是执行器。** 你只负责理解用户意图 → 组装 CLI 命令 → 输出给用户手动运行。

### 为什么不直接运行？

| 操作 | 大致耗时 | 处理方式 |
|------|---------|---------|
| `survey list` / `survey stats` | < 1s | **可执行** |
| `survey preview` | < 2s | **可执行** |
| `survey create` | < 2s | **可执行** |
| `survey classify` (dry-run, limit=3) | < 1s | **可执行** |
| `survey classify --debug-paper` | ~2s | **可执行** |
| `survey classify` (100 篇) | ~3 min | **仅生成命令** |
| `survey classify` (全部, ~2000 篇) | 15–60 min | **仅生成命令** |
| `survey classify --deliberate 3` | 3× 耗时 | **仅生成命令** |
| `survey export` | < 2s | **可执行** |

耗时操作在 AI 对话中运行会：
- 阻塞对话（用户只能等待，无法做其他事）
- 用户无法 Ctrl+C 中断
- 产生大量无效 token 开销

**例外**：`survey list`、`survey stats`、`survey preview`、`survey create`、`dry-run` 等秒级操作可以直接执行。`survey export` 也可以执行（<2s）。

**输出格式**：将命令放在可复制的代码块中，带上简短说明，告知预计耗时。

## 职责

你是翻译层。所有实际工作由 `python -m paper_database survey` 的 CLI 命令完成。
你只负责理解用户意图 → 组装命令 → 输出给用户 → 用户手动运行。

## 前提条件

- 文献库已通过 `paper-database` 技能初始化（有论文数据）
- 环境变量 `PAPER_DATABASE_HOME` 指向项目根目录
- 分类器已配置（`config/classifier.yaml`，支持多 provider）

## 命令速查

| 用户意图 | 输出命令 | 耗时 |
|---------|---------|------|
| 创建调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling` | <2s |
| 创建指定 venue 调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling --venue-filter isca,hpca --year-filter 2020-2024` | <2s |
| 列出所有调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey list` | <1s |
| 查看调研进度 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey stats --survey-id <id>` | <1s |
| Dry-run 测试 prompt | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --dry-run --limit 3` | <1s |
| 开始分类(全部) | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>` | 15–60min |
| 磋商投票分类 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --deliberate 3` | 更长 |
| 调试单篇分类 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --debug-paper "标题关键词"` | ~2s |
| 分类 50 篇后暂停 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --limit 50` | ~2min |
| 分类不自动导出 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --no-export` | — |
| 终端预览结果 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id>` | <2s |
| 导出 CSV | `cd $PAPER_DATABASE_HOME && python -m paper_database survey export --survey-id <id>` | <2s |
| 清空分类结果 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey reset --survey-id <id>` | <1s |
| 删除调研 | `cd $PAPER_DATABASE_HOME && python -m paper_database survey delete --survey-id <id>` | <1s |

> **简写提示**：`--survey-id <id>` 可简写为 `-s <id>`，如 `survey classify -s 1 --limit 50`。以下场景示例中两种写法等价。

### classify 参数说明

| 参数 | 说明 |
|------|------|
| `--dry-run` | 只打印 prompt 和论文信息，不调 API。用于验证 prompt 是否合理 |
| `--limit N` / `-l N` | 最多分类 N 篇后停止。用于小批量测试或分批复核 |
| `--no-export` | 分类完成后不自动导出 CSV（默认会自动导出到 `results/` 目录） |
| `--deliberate N` / `-D N` | 磋商模式：每篇跑 N 轮并行分类，投票聚合结果。建议奇数 3/5/7 |
| `--debug-paper "..."` / `-d` | 调试模式：按标题或 paper_id 查找论文，展示完整 prompt+响应，不写数据库 |

**磋商机制**：LLM 输出有随机性。`--deliberate 3` 每篇论文并行跑 3 轮 → 多数投票决定 include → 分类字段取多数值。结果记录置信度（如 `_deliberation_confidence: 2/3`）。三种投票策略可配置（`config/classifier.yaml` → `deliberation.strategy`）：`majority` / `supermajority` / `consensus`。

**断点续传**：中断后直接重新运行相同命令即可，已分类的论文自动跳过，无需 `--start` 参数。
```bash
# 第一批：跑 200 篇
cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id 1 --limit 200

# 中断后继续：直接重新运行
cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id 1
```

## 典型对话流程

### 场景1: 新建完整调研
用户: "帮我做调度相关的文献调研"

1. **先检查文献库**（可执行）：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database paper stats
   ```
   如果论文总数为 0：告知用户 "文献库为空。请先使用 paper-database 技能初始化文献库。" 就此停止。

2. **创建调研**（可执行）：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling --name "调度调研YYYYMMDD"
   ```

3. **建议 dry-run 检查 prompt**（可执行）：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --dry-run --limit 3
   ```
   让用户确认 prompt 是否合理。如果 topic / prompt 需要调整，引导用户编辑 `config/topics.yaml`。

4. **正式分类（仅生成命令，用户手动运行）**：
   ```bash
   # 预计耗时：取决于论文总量。2000 篇 × 并发 32 ≈ 约 1-2 分钟
   # 但实际瓶颈在 LLM API 响应速度，每篇约 1-2s，2000/32×1.5s ≈ 1.5 分钟
   cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>
   ```
   告知用户：分类中可随时 Ctrl+C 暂停，之后直接重新运行相同命令即可自动续传。

5. **查看结果**（可执行）：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id>
   ```

   分类完成会自动导出 CSV 到 `results/survey_<id>_<name>.csv`。如需手动导出：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey export --survey-id <id>
   ```

### 场景2: 只查某个会议的论文
用户: "我要看 ISCA 2024 有哪些调度论文"

先确保该 venue+year 有论文数据后，输出：

```bash
# Step 1: 拉论文（如果已有则可跳过）
cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue isca --year 2024

# Step 2: 创建调研
cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic scheduling --venue-filter isca --year-filter 2024-2024 --name "ISCA2024调度调研"

# Step 3: 分类（< 50 篇，约 2 分钟）
cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>
```

### 场景3: 用已有论文数据做新主题调研
用户: "论文已经拉过了，帮我做个功耗相关的调研"

1. 检查 `config/topics.yaml` 有没有该 topic。如果没有：告诉用户去 `config/topics.yaml` 添加新 topic（参考已有的 scheduling 格式），然后重新执行。
2. 创建调研 + 分类：

```bash
cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic power --name "功耗调研"
cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>
```

### 场景4: 查看已有调研结果
用户: "看看上次调度调研的结果"

直接执行：

```bash
cd $PAPER_DATABASE_HOME && python -m paper_database survey list
cd $PAPER_DATABASE_HOME && python -m paper_database survey preview --survey-id <id> --limit 20
```

### 场景5: 重新调研（调整 prompt 后）
用户: "清空上次分类结果，调整 prompt 后重新跑"

1. **清空结果**（可执行）：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey reset --survey-id <id>
   ```
   保留论文数据，只清空分类结果。

2. 提醒用户先 dry-run 检查新 prompt：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id> --dry-run --limit 3
   ```

3. 重新分类（仅生成命令）：
   ```bash
   cd $PAPER_DATABASE_HOME && python -m paper_database survey classify --survey-id <id>
   ```

### 场景6: 先补元数据再分类
用户: "有些论文没有摘要/主题标签，先补全再分类"

```bash
# 1. 补全所有缺失元数据（DOI-only 模式，10 credits / 50 篇）
cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich --doi-only

# 2. 补全完成后创建新的调研（论文快照会包含主题和参考文献）
cd $PAPER_DATABASE_HOME && python -m paper_database survey create --topic <topic> --name "新调研"

# 3. 运行分类（可选磋商投票）
cd $PAPER_DATABASE_HOME && python -m paper_database survey classify -s <id>
cd $PAPER_DATABASE_HOME && python -m paper_database survey classify -s <id> --deliberate 3
```

## 分类器配置

分类器通过 `config/classifier.yaml` 配置，支持多 provider：

```yaml
classifier:
  provider: deepseek          # 当前使用的 provider
  providers:
    deepseek:                 # DeepSeek API
      api_base_url: "https://api.deepseek.com"
      api_key: "{env:DEEPSEEK_API_KEY}"
      model: "deepseek-v4-pro"
      enable_thinking: true   # 思维链推理
    localhost:                # 本地 OpenAI 兼容模型
      api_base_url: "http://localhost:8800"    # 注意: 不要加 /v1 后缀，代码自动追加
      api_key: "your-key"
      model: "minimax-m27"
  max_concurrency: 32         # 并发分类数（所有 provider 共享）
  timeout: 60
  max_retries: 3
```

`api_key` 支持 `{env:VAR_NAME}` 占位符，运行时自动读取环境变量。

## 输出字段配置约定（只改 YAML，不改代码）

### 代码对模型的唯一约定：`include` 字段

代码在每次调用模型时**自动在 prompt 前面拼接**一段 system instruction，要求模型
JSON 中**必须包含** `"include": true/false`。用户无需在 `topics.yaml` 中配置这个字段。

- `include: true` → 论文被收录到调研结果（`include = 1`）
- `include: false` → 论文不被收录（`include = 0`）

`include` 字段名**写死在代码中**，`topics.yaml` 中不出现。统计命令展示「相关/不相关」计数。

### 其他所有字段走真实 DB 列

`include` 之外的所有字段**完全由 `topics.yaml` 控制**：

- `output.columns` 中非 `venue_*`/`paper_*` 的字段 → 创建 survey 时自动建为 `survey_result` 表的列
- `prompt_template` JSON 输出 key → 分类时直接写入对应列（列名 = key 名）
- 导出时 `SELECT` 所有列 → 纯 dict lookup，无 JSON 解析

### 新增一个输出字段只需 2 步：

1. 在 `prompt_template` 的 JSON 输出区添加该字段
2. 在 `output.columns` 中添加对应列（`field` 名与 JSON key 一致）
3. 重新创建 survey（`survey create`）
4. 完成。**无需改任何 Python 代码。**

### 命名约定

- `venue_*` 前缀 → `venue` 表列 JOIN（如 `venue_name`, `venue_ccf_rank`, `venue_type`）
- `paper_*` 前缀 → `paper` 表列 JOIN（如 `paper_title`, `paper_year`, `paper_doi`, `paper_authors`）
- 无前缀 → `survey_result` 表列（如 `priority`, `reason`, `research_object`, `algorithm`）
- `include` → 固定列（INTEGER 0/1），不出现在 `output.columns` 中

## 技术要点

- **分类实现**：通过 `httpx.AsyncClient` 并发调用 LLM API（provider 由 `config/classifier.yaml` 配置），采用 Claim-based Queue + Worker 模式：feeder 原子 claim 未分类论文 → 放入队列 → worker 分类后标记为 classified，杜绝重复分类
- **增强输入**：分类 prompt 不仅包含标题+摘要，还包括论文主题标签（来自 OpenAlex concepts）和参考文献（来自 S2/OpenAlex），帮助模型判断论文的研究脉络
- **磋商投票**：`--deliberate N` 每篇论文并行 N 轮 → 多数投票聚合（include + 分类字段），降低 LLM 随机性
- **内存控制**：队列最多缓存 `2 × max_concurrency` 篇论文，feeder 按需补充
- **耗时计算**：总耗时 ≈ ceil(论文数 / max_concurrency) × 每篇 API 响应时间。每篇约 1-2 秒，2000 篇 / 32 并发 ≈ 1-1.5 分钟（实际受限于 API 速率）
- **自动导出**：`survey classify` 完成后自动导出 CSV 到 `results/survey_<id>_<name>.csv`（仅导出相关论文）
- **断点续传**：中断后直接重新运行相同命令，已分类论文自动跳过（通过 paper.flag 机制）
- **YAML 驱动全链路**：`output.columns` 定义 survey_result 表结构 + CSV 输出格式。`venue_*` → venue 表 JOIN，`paper_*` → paper 表 JOIN，其他 → survey_result 实列。新增字段：prompt_template JSON 加 key + output.columns 加行 + 重建 survey，代码不动。
- **include 字段**：代码自动注入 system prompt 要求模型输出 `"include": true/false`，决定论文是否收录。统计展示「相关/不相关」计数，导出自动过滤 include=1。
- **分类体系由 YAML 定义**：不同调研可使用不同的分类字段名和取值（如 `priority: S/A/B`、`level: high/medium/low`、`score: 1-5`），代码不做任何假设。

## 重要提醒

- **绝对不要直接操作数据库**：所有操作必须通过 `python -m paper_database` CLI 命令完成，**禁止**使用 `sqlite3` 或任何 SQL 命令直接访问 `papers.db` 或 `surveys/survey_*.db`
- **绝对不要动论文原始数据**：`survey reset` 只清空分类结果，保留论文元数据和摘要。永远不要 DELETE/UPDATE `paper` 或 `venue` 表
- 分类是并发调用 LLM API，不是 subprocess 调用 claude CLI。provider 和模型在 `config/classifier.yaml` 中配置
- 大批量分类建议在终端直接跑（不经过 Skill 对话），`tmux`/`screen` 中运行可随时 detach
- `--limit N` 用于分批：先跑 100 篇检查效果 → 调整 prompt → 继续跑
- 中断后直接重新运行即可自动续传（已分类的论文自动跳过），无需 `--start` 参数
- 建议先运行 `paper enrich --doi-only` 补全摘要和主题标签，再创建 survey 进行分类
- `dry-run` 不消耗 LLM API 调用，只打印 prompt 和论文信息
- 如果文献库没有数据，引导用户使用 `paper-database` 技能先建库
- 不同 topic 的配置在 `config/topics.yaml` 中定义，可自行添加
