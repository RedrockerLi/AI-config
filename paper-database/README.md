# Paper Database — 文献库管理系统

从 DBLP、Semantic Scholar、OpenAlex 自动拉取论文元数据和摘要，通过本地 CLI LLM 工具逐篇判断是否与指定主题相关，导出 CSV。

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
python -m paper_database survey classify --survey-id 1 --dry-run --limit 3

# 6. 正式分类
python -m paper_database survey classify --survey-id 1 --limit 10

# 7. 预览 + 导出
python -m paper_database survey preview --survey-id 1 --relevant-only
python -m paper_database survey export --survey-id 1 --relevant-only
```

## 架构

```
用户
  ├─ 终端直接跑 CLI  ←── 模式A: 唯一的分类型执行路径
  │   python -m paper_database survey classify
  │        └─ classifier.py → subprocess → claude CLI → 写 SQLite
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

### `config/classifier.yaml` — CLI 工具 + prompt

切换 LLM 工具:
```yaml
# Claude
classifier:
  tool: claude
  cli_args: ["-p", "{prompt}", "--output-format", "json", "--max-tokens", "500"]

# Ollama
classifier:
  tool: ollama
  cli_args: ["run", "llama3", "{prompt}"]
```

## CLI 命令清单

```bash
# Venue
python -m paper_database venue init
python -m paper_database venue list

# Paper
python -m paper_database paper fetch
python -m paper_database paper fetch-abstracts
python -m paper_database paper fetch-all [--venue X --year Y]
python -m paper_database paper stats

# Survey
python -m paper_database survey create --topic scheduling [--name "..."]
python -m paper_database survey list
python -m paper_database survey stats --survey-id X
python -m paper_database survey delete --survey-id X

# Classify
python -m paper_database survey classify --survey-id X [--dry-run] [--limit N] [--start N]

# Export
python -m paper_database survey preview --survey-id X [--relevant-only]
python -m paper_database survey export --survey-id X [-o path/to/output.csv] [--relevant-only]
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

## Semantic Scholar API Key

设置环境变量以获得更高速率 (100 req/s vs 1 req/s):
```bash
export S2_API_KEY="your-api-key"
```
免费注册: https://www.semanticscholar.org/product/api

## 数据来源

| 步骤 | API | 获取内容 |
|------|-----|---------|
| ① 论文列表 | DBLP XML 导出 | title, authors, year, doi, dblp_key |
| ② 摘要(优先) | Semantic Scholar | abstract (纯文本), citations |
| ③ 摘要(备用) | OpenAlex | abstract (倒排索引需重构) |

DBLP XML 导出比搜索 API 更准确 — 搜索 API 存在子串匹配问题（如 `venue:MICRO` 会匹配到 Microprocessors 等无关期刊）。

## 数据库

`papers.db` (SQLite, gitignored):
- `venue` / `paper`: 论文元数据（与调研无关）
- `survey` / `survey_result`: 每次调研独立的分类结果

多机器使用: 拷贝 `papers.db` 或在每台机器上重新 `fetch-all`。
