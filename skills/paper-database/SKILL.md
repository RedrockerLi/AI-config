---
name: paper-database
description: 文献库管理 — 初始化会议/期刊 (venue)、从 DBLP 拉取论文数据、补全元数据(摘要/主题/参考文献)、查看统计。触发词: "初始化文献库", "拉论文", "更新论文", "文献库", "paper database", "/paper-database"。当用户想要设置或维护论文数据库时使用。
version: 0.3.0
---

## 核心规则

**你是命令生成器，不是执行器。** 你只负责理解用户意图 → 组装 CLI 命令 → 输出给用户手动运行。

### 为什么不直接运行？

| 操作 | 大致耗时 | 处理方式 |
|------|---------|---------|
| `venue init` / `venue list` | < 1s | **可执行** |
| `paper stats` | < 1s | **可执行** |
| `paper fetch` (单 venue 单年) | ~5s | **可执行** |
| `paper fetch` (全部 venue) | 10–30 min | **仅生成命令** |
| `paper enrich` | 10–60 min | **仅生成命令** |
| `paper fetch-all` | 30 min – 2h | **仅生成命令** |

耗时操作在 AI 对话中运行会：
- 阻塞对话（用户只能等待，无法做其他事）
- 用户无法 Ctrl+C 中断
- 产生大量无效 token 开销

**输出格式**：将命令放在可复制的代码块中，带上简短说明，告知用户预计耗时。

## 职责

你是翻译层。所有实际工作由 `python -m paper_database` 的 CLI 命令完成。
你只负责理解用户意图 → 组装命令 → 输出给用户 → 用户手动运行。

## 前提条件

- 项目已通过 `pip install -e .` 安装
- 环境变量 `PAPER_DATABASE_HOME` 指向项目根目录（通过 `setup.sh` 设置）
- 所有命令在 `$PAPER_DATABASE_HOME` 下执行

## 命令速查

| 用户意图 | 输出命令 | 耗时 |
|---------|---------|------|
| 初始化 venue 表 | `cd $PAPER_DATABASE_HOME && python -m paper_database venue init` | <1s |
| 列出所有 venue | `cd $PAPER_DATABASE_HOME && python -m paper_database venue list` | <1s |
| 拉取论文列表(全部) | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch` | 10–30min |
| 拉取论文列表(单 venue 单年) | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch --venue isca --year 2024` | ~5s |
| 拉取论文+元数据(全部) | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all` | 30min–2h |
| 拉取论文+元数据(指定) | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue hpca --year 2024` | ~30s |
| 补全元数据 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich` | 10–60min |
| 补全元数据+参考文献 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich --fetch-references` | 10–60min |
| 先补 100 篇测试 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich --stop-after 100` | ~5min |
| 查看论文统计 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats` | <1s |

### enrich 参数说明

- `--limit N` / `-l N`：每批处理数量（默认 10000）
- `--stop-after N` / `--max-total N`：最多处理多少篇后停止（0=不限制，直到全部完成）
- `--doi-only`：仅批量 DOI 查询 (10 credits/50 篇)，跳过昂贵的标题搜索
- `--fetch-references`：同时获取参考文献列表（S2 零额外调用; OpenAlex 需二阶段 API 调用）
- 自动检测三种缺失：摘要、主题标签、参考文献
- 支持断点续跑：中断后重新执行会从上次位置继续
- `fetch-abstracts` 保留为隐藏别名，自动转发到 `enrich`

## 典型对话流程

### 场景1: 全新建立文献库
用户: "帮我初始化文献库，拉取所有 CCF-A/B 体系结构论文"

**这是最耗时的操作**。输出以下命令让用户在终端手动执行：

```bash
# Step 1: 初始化 venue 表（秒级）
cd $PAPER_DATABASE_HOME && python -m paper_database venue init

# Step 2: 拉取所有论文 + 元数据（预计 30 分钟 - 2 小时）
#   涉及 20+ venue × 多年度 = 200+ DBLP 请求 + 数千 S2/OpenAlex 元数据查询
#   建议在 tmux/screen 中运行，可随时中断后重新执行续传
cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all

# Step 3: 查看统计
cd $PAPER_DATABASE_HOME && python -m paper_database paper stats
```

并告知用户：
- 建议先设置 API Key 加速元数据获取（见下方 API Keys 说明）
- 中断后重新运行会自动跳过已获取的论文和元数据
- `fetch-all` = `paper fetch` + `paper enrich`，可拆开执行

### 场景2: 更新特定会议的最新论文
用户: "把 ISCA 2025 和 MICRO 2025 的论文拉一下"

输出命令（每条约 30 秒）：

```bash
cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue isca --year 2025
cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue micro --year 2025
```

### 场景3: 检查文献库状态
用户: "看看库里有多少论文"

直接执行（秒级）：

```bash
cd $PAPER_DATABASE_HOME && python -m paper_database paper stats
```

然后汇报：总数、有摘要数量、各 venue 分年统计。

### 场景4: 补全缺失的元数据
用户: "有些论文没摘要/没主题标签，帮我补全"

先执行 `paper stats` 查看缺失数量，然后输出：

```bash
# 全部补全（检测所有缺失: 摘要+主题标签，可能耗时 10-60 分钟）
cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich

# 带参考文献一起
cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich --fetch-references

# 或者先只补 200 篇试试
cd $PAPER_DATABASE_HOME && python -m paper_database paper enrich --stop-after 200
```

### 场景5: 添加新 venue 后拉取
用户: "我在 config/venues.yaml 加了新会议，帮我拉数据"

输出：

```bash
# 先同步 venue 表
cd $PAPER_DATABASE_HOME && python -m paper_database venue init

# 拉取新 venue 的论文（替换 <venue-key>）
cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue <venue-key>
```

## API Keys（加速摘要获取）

| Key | 用途 | 获取地址 | 推荐度 |
|-----|------|---------|--------|
| `OPENALEX_API_KEY` | OpenAlex — 摘要+主题+参考文献ID (DOI批量50/批, 10 credits/批) | https://openalex.org/settings/api | 强烈推荐 |
| `S2_API_KEY` | Semantic Scholar — 摘要+参考文献标题 (DOI批量500/批) | https://www.semanticscholar.org/product/api | 可选补充 |

流程：**OpenAlex (主)** → **Semantic Scholar (补充)**。
无 API Key 也可用，OpenAlex 每天 100 credits（~10 次批量查询），强烈建议申请免费 Key。

```bash
export OPENALEX_API_KEY="your-key"
export S2_API_KEY="your-key"          # 可选
```

## 技术要点

- **数据来源**：DBLP XML（论文列表）→ OpenAlex（主：摘要+topics+refs ID）→ Semantic Scholar（补充：摘要+refs 标题）
- **参考文献存储**：paper_reference 存 `W<ID>` 短 ID + S2 标题分列；reference_work 作 ID→标题字典表；prompt 生成时 OpenAlex JOIN 优先，S2 fallback
- **`enrich` 自动检测缺失**：摘要、主题标签、参考文献三种缺失自动识别，已完整的论文自动跳过
- **多卷会议**：DBLP fetcher 自动从 `index.xml` 发现多卷（如 ASPLOS 2023 有 4 卷），无需手动处理
- **自适应限流**：OpenAlex 遇 429 自动降速（延迟翻倍），连续 3 次 429 后停止重试
- **分类器**：支持多 provider（`config/classifier.yaml`），当前支持 DeepSeek 和本地 OpenAI 兼容模型

## 重要提醒

- **绝对不要直接操作数据库**：所有操作必须通过 `python -m paper_database` CLI 命令完成，**禁止**使用 `sqlite3` 或任何 SQL 命令直接访问 `papers.db`
- **绝对不要动论文原始数据**：`paper fetch` 和 `paper enrich` 使用 INSERT OR IGNORE，不会覆盖已有数据。永远不要手动 DELETE/UPDATE `paper` 或 `venue` 表
- `fetch-all` 是 `fetch` + `enrich` 的组合，适合首次建库
- `fetch` 只拉论文列表不含摘要，适合快速更新
- 多机器使用：拷贝 `papers.db` 或在每台机器上重新 `fetch-all`
- 文献库就绪后，使用 `paper-survey` 技能做主题调研
