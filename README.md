# AI-config

个人 AI 工具配置仓库 — 跨 Claude Code / Hermes / Codex 的 Skills 脚本和工具集。

## 结构

```
AI-config/
├── setup.sh                  # 一键部署全部工具
├── scripts/                  # 各工具的独立部署脚本
│   └── setup-paper-database.sh
├── skills/                   # AI Skill 定义（硬链接源头）
│   ├── paper-database/       # 文献库管理
│   └── paper-survey/         # 文献调研
└── paper-database/           # 文献库管理工具 (Python CLI)
    ├── config/               # Venue / Topic / Classifier 配置
    ├── paper_database/       # Python 包
    └── README.md
```

## 快速开始

```bash
git clone <this-repo> ~/AI-config
cd ~/AI-config
./setup.sh
source ~/.bashrc
```

`setup.sh` 会自动完成：
1. `pip install -e .` 安装 Python 工具
2. 写入 `PAPER_DATABASE_HOME` 环境变量到 `~/.bashrc`
3. 发现并部署 Skills 到各 AI 工具目录（硬链接）

## Tools

| 工具 | 说明 |
|------|------|
| [paper-database](paper-database/) | 文献库管理 — DBLP 拉取论文 + AI 分类筛选 + CSV 导出 |

## Skills

| Skill | 用途 | 触发词 |
|-------|------|--------|
| `paper-database` | 文献库管理 — 初始化 venue、拉取论文 | "初始化文献库", "拉论文" |
| `paper-survey` | 主题调研 — AI 分类筛选、导出结果 | "文献调研", "论文筛选" |

Skills 通过硬链接同步到各 AI 工具，编辑 `skills/` 下源文件实时生效。

## 环境变量

paper-database 工具需要以下环境变量：

| 变量 | 用途 | 获取方式 |
|------|------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥（论文分类） | https://platform.deepseek.com/api_keys |
| `S2_API_KEY` | Semantic Scholar API 密钥（摘要获取加速，非必需） | https://www.semanticscholar.org/product/api |

DeepSeek API 配置（模型、并发数、思考模式等）在 `config/classifier.yaml` 中管理。详见 [paper-database README](paper-database/README.md)。

## 添加新工具

1. 在对应子目录开发工具
2. 编写 `scripts/setup-xxx.sh` 部署脚本
3. 在 `setup.sh` 的 `SETUP_SCRIPTS` 数组中注册
4. 在 `skills/` 下添加 Skill 定义
