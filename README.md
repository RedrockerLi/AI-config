# AI-config

个人 AI 工具配置仓库 — 跨 Claude Code / Hermes / Codex 的 Skills 和工具集。

## 快速开始

```bash
git clone --recurse-submodules <this-repo> ~/AI-config
cd ~/AI-config
./setup.sh && source ~/.bashrc
```

> `paper-database` 以 submodule 引入；若已 clone 未带 `--recurse-submodules`，执行 `git submodule update --init --recursive`。

## 内容

**[paper-database](paper-database/)** — 文献库管理系统。从 DBLP 拉取论文 → OpenAlex / Semantic Scholar 补全元数据（摘要、主题标签、参考文献）→ LLM 并发分类筛选，支持磋商投票，导出 CSV。[→ 详细文档](paper-database/README.md)（独立仓库 [RedrockerLi/Paper-Database](https://github.com/RedrockerLi/Paper-Database)，本仓库以 submodule 引用）

**Skills** — AI 工具的指令文件，通过 `setup.sh` 硬链接部署：

| Skill | 用途 |
|-------|------|
| `paper-database` | 文献库维护 — 初始化 venue、拉取论文、补全元数据 |
| `paper-survey` | 主题调研 — AI 分类筛选、磋商投票、导出 CSV |

> Skills 定义在 [skills/](skills/) 目录，编辑源文件实时生效。分类器支持多 provider（DeepSeek / 本地模型），API Key 配置详见 [paper-database README](paper-database/README.md)。
