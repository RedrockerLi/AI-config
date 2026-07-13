---
name: paper-database
description: 文献库管理 — 初始化会议/期刊 (venue)、从 DBLP 拉取论文数据、补全摘要、查看统计。触发词: "初始化文献库", "拉论文", "更新论文", "文献库", "paper database", "/paper-database"。当用户想要设置或维护论文数据库时使用。
version: 0.1.0
---

## 职责

你是翻译层。所有实际工作由 `python -m paper_database` 的 CLI 命令完成。
你只负责理解用户意图 → 执行对应命令 → 汇报结果。

## 前提条件

- 项目已通过 `pip install -e .` 安装
- 环境变量 `PAPER_DATABASE_HOME` 指向项目根目录（通过 `setup.sh` 设置）
- 所有命令在 `$PAPER_DATABASE_HOME` 下执行

## 命令速查

| 用户意图 | 执行命令 |
|---------|---------|
| 初始化 venue 表 | `cd $PAPER_DATABASE_HOME && python -m paper_database venue init` |
| 列出所有 venue | `cd $PAPER_DATABASE_HOME && python -m paper_database venue list` |
| 拉取论文列表 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch` |
| 拉取论文+摘要 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all` |
| 只拉某 venue 某年 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue isca --year 2024` |
| 补全摘要 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-abstracts` |
| 查看论文统计 | `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats` |

## 典型对话流程

### 场景1: 全新建立文献库
用户: "帮我初始化文献库，拉取所有 CCF-A/B 体系结构论文"

1. `cd $PAPER_DATABASE_HOME && python -m paper_database venue init`
2. `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all`
   - 这一步耗时较长（约 20+ venue × 10 年 = 200+ 次 DBLP 请求 + S2/OpenAlex 摘要查询）
   - 建议在终端直接执行，不经过对话（避免 token 开销）
3. `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats`
4. 汇总结果告诉用户

### 场景2: 更新特定会议的最新论文
用户: "把 ISCA 2025 和 MICRO 2025 的论文拉一下"

1. `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue isca --year 2025`
2. `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-all --venue micro --year 2025`
3. `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats`

### 场景3: 检查文献库状态
用户: "看看库里有多少论文"

1. `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats`
2. 汇报总数、有摘要数量、各 venue 分年统计

### 场景4: 只补全缺失的摘要
用户: "有些论文没摘要，帮我补全"

1. `cd $PAPER_DATABASE_HOME && python -m paper_database paper stats` → 看有多少缺失
2. `cd $PAPER_DATABASE_HOME && python -m paper_database paper fetch-abstracts`
3. 汇报结果

## 重要提醒

- 数据来源：DBLP (论文列表) → Semantic Scholar (摘要优先) → OpenAlex (备用)
- Semantic Scholar API Key 可加速摘要获取: `export S2_API_KEY="your-key"` (100 req/s vs 1 req/s)
- `fetch-all` 是 `fetch` + `fetch-abstracts` 的组合操作，适合首次建库
- `fetch` 只拉论文列表不含摘要，适合快速更新
- 文献库就绪后，使用 `paper-survey` 技能做主题调研