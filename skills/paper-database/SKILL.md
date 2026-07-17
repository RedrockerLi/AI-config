---
name: paper-database
description: 文献库管理 — 初始化会议/期刊 (venue)、从 DBLP 拉取论文数据、补全元数据(摘要/主题/参考文献)、查看统计、AI 翻译摘要。触发词: "初始化文献库", "拉论文", "更新论文", "文献库", "paper database", "/paper-database"。当用户想要设置或维护论文数据库时使用。
version: 0.4.0
---

## 核心规则

**你是命令生成器，不是执行器。** 秒级操作可直接执行，分钟/小时级操作只生成命令给用户手动运行。

| 操作 | 耗时 | 方式 |
|------|------|------|
| `venue init/list`, `paper stats` | <1s | **可执行** |
| `paper fetch` (单 venue 单年) | ~5s | **可执行** |
| `paper fetch` (全部) | 10–30min | **生成命令** |
| `paper enrich` | 10–60min | **生成命令** |
| `paper fetch-all` | 30min–2h | **生成命令** |
| `paper translate` | 分钟级 | **生成命令** |

## 前提

- `pip install -e .` 已安装
- `$PAPER_DATABASE_HOME` 指向项目根目录（`setup.sh` 设置）
- 所有命令在 `$PAPER_DATABASE_HOME` 下执行

## 命令速查

| 用户意图 | 命令 | 耗时 |
|---------|------|------|
| 初始化 venue | `python -m paper_database venue init` | <1s |
| 列出 venue | `python -m paper_database venue list` | <1s |
| 拉全部论文 | `python -m paper_database paper fetch` | 10–30min |
| 拉指定 venue/年 | `python -m paper_database paper fetch --venue isca --year 2024` | ~5s |
| 拉论文+元数据(全部) | `python -m paper_database paper fetch-all` | 30min–2h |
| 拉论文+元数据(指定) | `python -m paper_database paper fetch-all --venue hpca --year 2024` | ~30s |
| 补全元数据 | `python -m paper_database paper enrich` | 10–60min |
| 补全元数据(先试 100 篇) | `python -m paper_database paper enrich --stop-after 100` | ~5min |
| 补全+参考文献 | `python -m paper_database paper enrich --fetch-references` | 10–60min |
| 查看统计 | `python -m paper_database paper stats` | <1s |
| 翻译摘要(全部) | `python -m paper_database paper translate` | 分钟级 |
| 翻译摘要(100 篇) | `python -m paper_database paper translate --limit 100` | ~5min |

## 典型流程

### 全新建库

```bash
cd $PAPER_DATABASE_HOME
python -m paper_database venue init
python -m paper_database paper fetch-all    # 预计 30min–2h，建议 tmux 中跑
python -m paper_database paper stats
```

中断后重新运行自动跳过已获取的数据。

### 更新特定会议

```bash
cd $PAPER_DATABASE_HOME
python -m paper_database paper fetch-all --venue isca --year 2025
```

### 添加新 venue

```bash
cd $PAPER_DATABASE_HOME
python -m paper_database venue init                        # 同步 config/venues.yaml
python -m paper_database paper fetch-all --venue <key>     # 拉数据
```

## API Keys

| Key | 来源 | 推荐度 |
|-----|------|--------|
| `OPENALEX_API_KEY` | https://openalex.org/settings/api | 强烈推荐 |
| `S2_API_KEY` | https://www.semanticscholar.org/product/api | 可选 |

```bash
export OPENALEX_API_KEY="your-key"
export S2_API_KEY="your-key"     # 可选
```

流程：OpenAlex(主) → S2(补充)。无 Key 也可用（OpenAlex 每天 100 free credits）。

## 重要提醒

- **禁止直接操作数据库** — 所有操作通过 CLI 命令完成
- `fetch`/`enrich` 使用 `INSERT OR IGNORE`，不会覆盖已有数据
- `fetch-all` = `fetch` + `enrich`，首次建库用
- `fetch` 只拉论文列表，适合快速更新
- 文献库就绪后，用 `paper-survey` 技能做主题调研
