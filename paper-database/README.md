# Paper Database — 文献库管理系统

从 DBLP、OpenAlex、Semantic Scholar 自动拉取论文元数据、摘要、主题标签、参考文献，通过 LLM API 并发分类筛选，支持多轮磋商投票，导出 CSV。

## 快速开始

```bash
pip install -e .
python -m paper_database venue init
python -m paper_database paper fetch-all --venue hpca --year 2024   # 拉论文+元数据
python -m paper_database survey create --topic scheduling --name "测试调研"
python -m paper_database survey classify -s 1 --dry-run --limit 3   # 先检查 prompt
python -m paper_database survey classify -s 1 --limit 10            # 正式分类
python -m paper_database survey classify -s 1 --deliberate 3        # 磋商投票
python -m paper_database survey preview -s 1
python -m paper_database survey export -s 1
```

## 架构

```
用户 ─┬─ 终端直接跑 CLI      → classifier.py → httpx.AsyncClient → LLM API → SQLite
      └─ AI 工具 (Skills)    → 翻译成上述 CLI 命令输出给用户
```

AI 工具不直接执行耗时命令，而是生成命令供用户在终端运行。

## 配置

三个 YAML 文件在 `config/` 目录：

**`venues.yaml`** — 检索的会议/期刊。预填 CCF-A 体系结构 8 个 venue + CCF-B 13 个 venue。

```yaml
venues:
  - key: isca
    name: "International Symposium on Computer Architecture"
    type: conference
    ccf_rank: A
    dblp_url_prefix: "conf/isca"
    year_start: 2016
    year_end: 2026
```

**`topics.yaml`** — 调研主题定义（keywords、prompt_template、output.columns）。可添加多个 topic。

**`classifier.yaml`** — 分类器多 provider 配置 + 磋商策略：

```yaml
classifier:
  provider: deepseek         # 当前使用的 provider
  providers:
    deepseek:
      api_base_url: "https://api.deepseek.com"
      api_key: "{env:DEEPSEEK_API_KEY}"    # {env:VAR} 自动读取环境变量
      model: "deepseek-v4-pro"
      enable_thinking: true
    localhost:
      api_base_url: "http://localhost:8800"  # 不要加 /v1 后缀
      api_key: "your-key"
      model: "minimax-m27"
  max_concurrency: 32
  timeout: 60
  max_retries: 3
  deliberation:               # 磋商投票策略
    strategy: majority        # majority | supermajority | consensus
    rounds: 3
```

`api_key` 支持 `{env:VAR_NAME}` 占位符。`api_base_url` 不要加 `/v1` 后缀（代码自动追加 `/v1/chat/completions`）。

## CLI 命令

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
python -m paper_database survey create --topic scheduling [--name "..."] [--venue-filter ...] [--year-filter ...]
python -m paper_database survey list
python -m paper_database survey stats -s X
python -m paper_database survey delete -s X

# Classify
python -m paper_database survey classify -s X [--dry-run] [--limit N] [--no-export] [--deliberate N]
python -m paper_database survey classify -s X --debug-paper "title"

# Export
python -m paper_database survey preview -s X
python -m paper_database survey export -s X [-o output.csv]
python -m paper_database survey reset -s X
```

## 分类特性

### 磋商机制 (Deliberation)

LLM 输出有随机性。`--deliberate N` 每篇论文并行跑 N 轮分类，投票聚合结果：

```bash
python -m paper_database survey classify -s 1 --deliberate 3
python -m paper_database survey classify -s 1 --debug-paper "CGRA" --deliberate 3
```

三种投票策略（`config/classifier.yaml` → `deliberation.strategy`）：

| 策略 | 规则 |
|------|------|
| `majority` | 多数决，平局→收录 |
| `supermajority` | 赞成率 ≥ 0.67 才收录 |
| `consensus` | 全票通过才收录 |

结果记录置信度（如 `_deliberation_confidence: 2/3`）。

### 增强输入 (Topics + References)

分类 prompt 不仅含标题+摘要，还包括主题标签和参考文献标题，比只看摘要更能判断研究脉络。数据来源：

| 数据类型 | 来源 | 额外开销 |
|---------|------|:--:|
| 摘要 | OpenAlex / S2 | — |
| 主题标签 (concepts) | OpenAlex | 零 |
| 参考文献标题 | S2 优先 → OpenAlex 兜底 | 零 (S2) / 二阶段 API (OpenAlex) |

参考文献通过 `reference_work` 缓存表去重，同一篇被引论文全局只解析一次。

## API Keys

### 分类

在 `config/classifier.yaml` 中配置各 provider 的 `api_key`，支持 `{env:VAR_NAME}` 占位符。

### 元数据补全 (`enrich`)

流程：**OpenAlex (主)** → **Semantic Scholar (补充)**。

| Key | 用途 | 获取 |
|-----|------|------|
| `OPENALEX_API_KEY` | OpenAlex — 摘要 + concepts + 参考文献 ID | https://openalex.org/settings/api |
| `S2_API_KEY` | Semantic Scholar — 摘要 + 参考文献标题 | https://www.semanticscholar.org/product/api |

```bash
export OPENALEX_API_KEY="your-key"
export S2_API_KEY="your-key"          # 可选
```

```bash
python -m paper_database paper enrich                    # 补全所有缺失
python -m paper_database paper enrich --fetch-references # 同时获取参考文献
python -m paper_database paper enrich --doi-only         # 仅批量查询，低成本
```

`enrich` 自动检测三种缺失（摘要、主题标签、参考文献），已完整的自动跳过，支持断点续跑。`fetch-abstracts` 保留为隐藏别名。

## 数据库

`papers.db` (SQLite, gitignored):

| 表 | 内容 |
|----|------|
| `venue` / `paper` | 论文元数据 |
| `paper_topic` | 主题标签（OpenAlex concepts） |
| `paper_reference` | 参考文献列表（W+ID + S2 标题） |
| `reference_work` | ID→标题字典表（引用去重） |
| `survey` / `survey_result` | 调研分类结果 |

多机器使用：拷贝 `papers.db` 或在每台机器上重新 `fetch-all`。
