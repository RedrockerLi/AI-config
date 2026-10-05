# AI-config

个人 AI 工具配置仓库 — 跨 Claude Code / Hermes / Codex 的 Skills 和工具集。

## 快速开始

```bash
git clone --recurse-submodules <this-repo> ~/AI-config
cd ~/AI-config
./setup.sh && source ~/.bashrc
```

## 内容

**paper-database** — 文献库管理系统。从 DBLP 拉取论文 → OpenAlex / Semantic Scholar 补全元数据（摘要、主题标签、参考文献）→ LLM 并发分类筛选，支持磋商投票，导出 CSV。[→ 详细文档](paper-database/README.md)（独立仓库 [RedrockerLi/Paper-Database](https://github.com/RedrockerLi/Paper-Database)，本仓库以 submodule 引用）

**Skills** — AI 工具的指令文件，通过 `setup.sh` 硬链接部署：

| Skill | 用途 |
|-------|------|
| `writing-like-human` | 文献库维护 — 初始化 venue、拉取论文、补全元数据 |
| `workflow` | 多 agent 协作心智模型 — 任务分解、局部自主、共享语义与整体交付 |

workflow 的原则参考 Frederick P. Brooks Jr.《人月神话》，并结合 [agent 系统的工程实践](https://www.anthropic.com/engineering/multi-agent-research-system)、[协作扩展性研究](https://arxiv.org/abs/2512.08296)与[失败分析](https://arxiv.org/abs/2503.13657)作面向 agent 的转化。
