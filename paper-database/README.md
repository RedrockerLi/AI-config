# Paper Database — 文献库管理系统

从 DBLP、Semantic Scholar、OpenAlex 自动拉取论文元数据、摘要、主题标签、参考文献，通过 LLM API 并发分类筛选，支持多轮磋商投票，导出 CSV。

## 快速开始

```bash
# 1. 安装依赖
pip install -e .

# 2. 初始化 venue
python -m paper_database venue init

# 3. 拉取论文 (以 HPCA 2024 为例测试)
python -m paper_database paper fetch-all --venue hpca --year 2024

# 4. 创建调研
python -m paper_database survey create --topic scheduling --name "测试调研"

# 5. 先 dry-run 检查 prompt
python -m paper_database survey classify -s 1 --dry-run --limit 3

# 6. 正式分类 (支持磋商投票)
python -m paper_database survey classify -s 1 --limit 10
python -m paper_database survey classify -s 1 --deliberate 3   # 3轮磋商投票

# 7. 预览 + 导出
python -m paper_database survey preview -s 1python -m paper_database survey export -s 1```

## 架构

```
用户
  ├─ 终端直接跑 CLI  ←── 模式A: 唯一的分类型执行路径
  │   python -m paper_database survey classify
  │        └─ classifier.py → httpx.AsyncClient → DeepSeek API → 写 SQLite
  │
  └─ AI 工具 (Claude Code / Hermes / Codex)  ←── 模式B: 翻译层
      /paper-database venue init
      /paper-survey create --topic scheduling
           └─ Skill 翻译成上面的 CLI 命令
```

## 配置

### `config/venues.yaml` — 要检索的会议/期刊

预填了 CCF-A 系统结构全部 8 个 venue（10年）+ CCF-B 系统结构全部 13 个 venue（5年）。

添加新 venue 格式:
```yaml
venues:
  - key: my-venue
    name: "Full Venue Name"
    type: conference      # conference | journal
    ccf_rank: A
    dblp_url_prefix: "conf/my-abbrev"  # DBLP URL 路径，去 dblp.org 确认
    year_start: 2016
    year_end: 2026
```

DBLP XML 导出 URL 模式:
- 会议: `https://dblp.org/db/{dblp_url_prefix}/{abbrev}{year}.xml`
- 期刊: `https://dblp.org/db/{dblp_url_prefix}/{abbrev}.xml` (含所有年份)

### `config/topics.yaml` — 调研主题 + 输出列定义

可以添加多个 topic。`keywords` 用于构造分类 prompt。`output.columns` 定义 CSV 导出列和转换规则。

### `config/classifier.yaml` — 分类器配置（多 provider 支持）

```yaml
classifier:
  # ── 当前使用的 provider ──
  provider: deepseek          # deepseek | localhost | 任意 providers 中的 key

  # ── Provider 配置列表 ──
  providers:
    deepseek:
      api_base_url: "https://api.deepseek.com"
      api_key: "{env:DEEPSEEK_API_KEY}"     # {env:VAR} 占位符自动解析
      model: "deepseek-v4-pro"
      max_tokens: 800
      temperature: 0.0
      enable_thinking: true                  # 思维链推理

    localhost:                               # 本地 / 兼容 OpenAI 的模型
      api_base_url: "http://localhost:8800/v1"
      api_key: "your-key-here"
      model: "minimax-m27"
      enable_thinking: false

  # ── 通用设置 (所有 provider 共享) ──
  max_concurrency: 32      # 并发分类数
  timeout: 60
  max_retries: 3
```

- `provider` 切换当前使用的 LLM，`providers` 下可配置多个备选
- `api_key` 支持 `{env:VAR_NAME}` 占位符，运行时自动读取环境变量

## CLI 命令清单

```bash
# Venue
python -m paper_database venue init
python -m paper_database venue list

# Paper
python -m paper_database paper fetch
python -m paper_database paper enrich [--doi-only] [--stop-after N] [--fetch-references]
python -m paper_database paper fetch-all [--venue X --year Y]
python -m paper_database paper stats

# Survey
python -m paper_database survey create --topic scheduling [--name "..."]
python -m paper_database survey list
python -m paper_database survey stats --survey-id X
python -m paper_database survey delete --survey-id X

# Classify
python -m paper_database survey classify -s X [--dry-run] [--limit N] [--no-export] [--deliberate N]
python -m paper_database survey classify -s X --debug-paper "title"  # 调试单篇分类

# Export
python -m paper_database survey preview --survey-id X
python -m paper_database survey export --survey-id X [-o path/to/output.csv]
```

## AI Skills

项目提供了两个 Skill，可跨多种 AI 工具（Claude Code、Hermes、Codex）使用：

| Skill | 用途 | 触发词 |
|-------|------|--------|
| `paper-database` | 文献库管理 — 初始化 venue、拉取论文、补全摘要 | "初始化文献库", "拉论文" |
| `paper-survey` | 主题调研 — 创建调研、AI 分类、导出结果 | "文献调研", "论文筛选" |

通过 `setup.sh` 一键部署 Skills 到各 AI 工具：

```bash
cd /path/to/AI-config
./setup.sh                        # 自动发现所有 AI 工具
./setup.sh ~/.claude/skills       # 手动指定目录
```

## 分类特性

### 磋商机制 (Deliberation)

LLM 输出有随机性，单次分类可能不可靠。`--deliberate N` 启用磋商模式：

```bash
# 每篇论文并行跑 3 轮分类，投票决定最终结果
python -m paper_database survey classify -s 1 --deliberate 3

# 调试模式查看每轮详情
python -m paper_database survey classify -s 1 --debug-paper "CGRA" --deliberate 3
```

三种投票策略（`config/classifier.yaml` → `deliberation.strategy`）：

| 策略 | 规则 |
|------|------|
| `majority` | 多数决，平局→收录（"宁可多收录"） |
| `supermajority` | 赞成率 ≥ 阈值（默认 0.67）才收录 |
| `consensus` | 全票通过才收录，否则标记 uncertain |

### 增强输入 (Topics + References)

分类 prompt 不仅包含标题+摘要，还包括论文的**主题标签**和**参考文献**：

```
论文主题标签: Computer Architecture; Scheduling; CGRA; FPGA
引用文献（该论文引用的关键相关工作）:
  - Gandiva: Introspective Cluster Scheduling
  - Tetrisched: Global Scheduling with Constraints
  - Chronus: A Deadline-Aware Scheduler
```

这比只看摘要更能判断论文的研究脉络。数据来源：

| 数据类型 | 来源 | 额外 API 开销 |
|---------|------|:--:|
| 摘要 | S2 / OpenAlex | — |
| 主题标签 (concepts) | OpenAlex | 零（已在响应中） |
| 参考文献 (references) | S2（优先）/ OpenAlex | 零（S2）/ 二阶段批量（OpenAlex） |

## API Keys

### 分类

支持多 provider，在 `config/classifier.yaml` 的 `providers` 中配置各 provider 的 `api_key`。
支持 `{env:VAR_NAME}` 占位符。详见上方配置说明。

### 元数据补全 (`enrich`)

`enrich` 命令自动检测并补全所有缺失的元数据：摘要、主题标签、参考文献。

流程：**OpenAlex (主)** → **Semantic Scholar (S2_API_KEY 设后补充)**。

| Key | 用途 | 获取 | 推荐度 |
|-----|------|------|--------|
| `OPENALEX_API_KEY` | OpenAlex — 摘要 + concepts (主题标签) + referenced_works (参考文献ID) | https://openalex.org/settings/api | **强烈推荐** |
| `S2_API_KEY` | Semantic Scholar — 摘要 + references (直接给标题) | https://www.semanticscholar.org/product/api | 可选补充 |

```bash
export OPENALEX_API_KEY="your-key"
export S2_API_KEY="your-key"          # 可选
```

### enrich 使用

```bash
# 补全所有缺失 (摘要 + topics，自动续跑)
python -m paper_database paper enrich

# 同时获取参考文献 (S2 零额外开销)
python -m paper_database paper enrich --fetch-references

# DOI-only 模式 (仅批量查询，跳过标题搜索)
python -m paper_database paper enrich --doi-only
```

`fetch-abstracts` 保留为隐藏别名，自动转发到 `enrich`。

## 数据来源

| 步骤 | API | 获取内容 | 备注 |
|------|-----|---------|------|
| ① 论文列表 | DBLP XML 导出 | title, authors, year, doi, dblp_key | 比搜索 API 更准确 |
| ② 摘要 + topics + refs(主) | OpenAlex | abstract, concepts (主题标签), referenced_works (参考文献 ID) | DOI 批量 50/批, 10 credits/批 |
| ③ 摘要 + refs(补充) | Semantic Scholar | abstract, references (直接给标题) | 需 S2_API_KEY, DOI 批量 500/批 |

### enrich 两种模式

| 模式 | 命令 | 消耗 | 适用场景 |
|------|------|------|---------|
| **DOI-only** (推荐) | `enrich --doi-only` | 10 credits / 50 篇 | 快速低成本获取大量数据 |
| 完整模式 | `enrich` | DOI 批量 + 标题搜索 10 credits/篇 | 覆盖无 DOI 的论文 |

## 数据库

`papers.db` (SQLite, gitignored):
- `venue` / `paper`: 论文元数据
- `paper_topic`: 论文主题标签（来自 OpenAlex concepts）
- `paper_reference`: 论文参考文献列表（来自 S2 / OpenAlex）
- `survey` / `survey_result`: 每次调研独立的分类结果

多机器使用: 拷贝 `papers.db` 或在每台机器上重新 `fetch-all`。
