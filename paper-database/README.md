# Paper Database — 文献库管理系统

从 DBLP、Semantic Scholar、OpenAlex 自动拉取论文元数据和摘要，通过 DeepSeek API 并发判断是否与指定主题相关，导出 CSV。

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

# 6. 正式分类 (--fetch-abstracts 自动补摘要，可选)
python -m paper_database survey classify -s 1 --limit 10 --fetch-abstracts

# 7. 预览 + 导出
python -m paper_database survey preview -s 1 --relevant-only
python -m paper_database survey export -s 1 --relevant-only
```

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
python -m paper_database paper fetch-abstracts
python -m paper_database paper fetch-all [--venue X --year Y]
python -m paper_database paper stats

# Survey
python -m paper_database survey create --topic scheduling [--name "..."]
python -m paper_database survey list
python -m paper_database survey stats --survey-id X
python -m paper_database survey delete --survey-id X

# Classify
python -m paper_database survey classify -s X [--dry-run] [--limit N] [--start N] [--fetch-abstracts]

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

## API Keys

### 分类

支持多 provider，在 `config/classifier.yaml` 的 `providers` 中配置各 provider 的 `api_key`。
支持 `{env:VAR_NAME}` 占位符。详见上方配置说明。

### 摘要获取

摘要获取流程：**Semantic Scholar (Phase 1)** → **OpenAlex 批量 (Phase 2)** 兜底。

| Key | 用途 | 获取 | 推荐度 |
|-----|------|------|--------|
| `S2_API_KEY` | Semantic Scholar — DOI 批量 + 标题搜索 | https://www.semanticscholar.org/product/api | 推荐 |
| `OPENALEX_API_KEY` | OpenAlex — DOI 批量查询（50 篇/批，10 credits/批） | https://openalex.org/settings/api | 申请方便，**强烈推荐** |

```bash
export S2_API_KEY="your-key"
export OPENALEX_API_KEY="your-key"
```

## 数据来源

| 步骤 | API | 获取内容 | 备注 |
|------|-----|---------|------|
| ① 论文列表 | DBLP XML 导出 | title, authors, year, doi, dblp_key | 比搜索 API 更准确 |
| ② 摘要(优先) | Semantic Scholar | abstract (纯文本), citations | 支持 DOI 批量 (500/批) |
| ③ 摘要(兜底) | OpenAlex | abstract (倒排索引) | **DOI 批量 50/批, 10 credits/批** |

DBLP XML 导出比搜索 API 更准确 — 搜索 API 存在子串匹配问题（如 `venue:MICRO` 会匹配到 Microprocessors 等无关期刊）。

OpenAlex 批量优化: 有 DOI 的论文自动 50 篇一组合并为一个 API 请求（pipe 分隔），
相比逐篇查询节省 ~80% credits。无 DOI 的论文回退到标题搜索。

## 数据库

`papers.db` (SQLite, gitignored):
- `venue` / `paper`: 论文元数据（与调研无关）
- `survey` / `survey_result`: 每次调研独立的分类结果

多机器使用: 拷贝 `papers.db` 或在每台机器上重新 `fetch-all`。
