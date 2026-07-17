# Paper Database — 文献库管理系统

从 DBLP 拉论文 → OpenAlex / Semantic Scholar 补全元数据 → LLM 并发分类筛选 → 导出 CSV / Markdown。

## 快速开始

```bash
pip install -e .
python -m paper_database venue init
python -m paper_database paper fetch-all                     # 拉全部论文+元数据
python -m paper_database survey create --topic scheduling    # 创建调研
python -m paper_database survey classify -s 1 --dry-run --limit 3
python -m paper_database survey classify -s 1               # 正式分类
python -m paper_database survey preview -s 1
python -m paper_database survey export -s 1                  # CSV
python -m paper_database survey export-md -s 1               # Markdown
```

## CLI 命令

```bash
# Venue
python -m paper_database venue init                          # 同步 venues.yaml → DB
python -m paper_database venue list

# Paper
python -m paper_database paper fetch [-v venue] [-y year]    # DBLP 论文列表
python -m paper_database paper enrich [--doi-only] [--fetch-references]
python -m paper_database paper fetch-all [-v venue] [-y year]  # fetch + enrich
python -m paper_database paper stats
python -m paper_database paper translate [--limit N]         # AI 摘要英→中

# Survey
python -m paper_database survey create -t topic [-n name] [--venue-filter ...] [--year-filter ...]
python -m paper_database survey list
python -m paper_database survey stats -s X
python -m paper_database survey classify -s X [--dry-run] [--limit N] [--deliberate N]
python -m paper_database survey classify -s X -d "title"     # 调试单篇
python -m paper_database survey preview -s X
python -m paper_database survey export -s X [-o output]      # CSV
python -m paper_database survey export-md -s X [-o dir]      # Markdown 按 venue 分文件
python -m paper_database survey translate -s X [--limit N]   # 翻译入选摘要
python -m paper_database survey reset -s X                   # 清空分类，保留论文
python -m paper_database survey delete -s X
```

### 磋商投票

```bash
python -m paper_database survey classify -s 1 --deliberate 3   # 每篇并行 3 轮投票
```

策略配置在 `config/llm.yaml`：`majority` / `supermajority` / `consensus`。

## Markdown 导出

每个 venue 一个 `.md` 文件，`#` = venue 名，`##` = 年份，论文按 YAML 中 `rank` 字段排序：

```bash
python -m paper_database survey export-md -s 1      # → results/survey_1_md/*.md
```

## 配置

三个 YAML 文件在 `config/`：

| 文件 | 内容 |
|------|------|
| `venues.yaml` | 会议/期刊定义，预填 22 个 CCF-A/B venue |
| `topics.yaml` | 调研主题：prompt template + 输出字段 (columns) |
| `llm.yaml` | LLM provider 多配置 + 磋商策略 |

**分类器示例：**

```yaml
llm:
  provider: deepseek
  providers:
    deepseek:
      api_base_url: "https://api.deepseek.com"
      api_key: "{env:DEEPSEEK_API_KEY}"   # 自动读环境变量
      model: "deepseek-v4-pro"
      enable_thinking: true
    localhost:
      api_base_url: "http://localhost:8800"
      model: "minimax-m27"
  max_concurrency: 32
```

`api_base_url` 不加 `/v1` 后缀（代码自动追加 `/v1/chat/completions`）。

## API Keys

| Key | 来源 | 用途 |
|-----|------|------|
| `OPENALEX_API_KEY` | [openalex.org](https://openalex.org/settings/api) | 摘要 + concepts + 参考文献 |
| `S2_API_KEY` | [semanticscholar.org](https://www.semanticscholar.org/product/api) | 摘要 + 参考文献标题（补充） |
| `DEEPSEEK_API_KEY` | [deepseek.com](https://platform.deepseek.com) | LLM 分类 |

流程：OpenAlex(主) → S2(补充)。无 Key 也可用（OpenAlex 每天 100 free credits）。

## 数据库

`papers.db` (SQLite, gitignored) — `venue` / `paper` / `paper_topic` / `reference_work`。

每个 survey 独立 `surveys/survey_N.db`（论文快照 + 分类结果），结构由 topics.yaml 动态定义。
