"""CLI entry point for paper-database framework.

Usage:
    python -m paper_database venue init
    python -m paper_database paper fetch-all
    python -m paper_database survey create --topic scheduling
    python -m paper_database survey classify --survey-id 1
    python -m paper_database survey export --survey-id 1
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table as RichTable

from paper_database.classifier import CLIClassifier
from paper_database.config import get_config, reload_config, TopicConfig
from paper_database.db import Database
from paper_database.exporter import Exporter
from paper_database.fetcher.base import PaperMeta
from paper_database.fetcher.dblp import DBLPFetcher
from paper_database.fetcher.openalex import OpenAlexFetcher
from paper_database.fetcher.semantic_scholar import SemanticScholarFetcher

console = Console()


# ── Helpers ─────────────────────────────────────────────────────

def _get_db(db_path: str = "papers.db") -> Database:
    """Get a Database instance and ensure tables exist."""
    db = Database(db_path)
    db.init_db()
    return db


def _resolve_config(config_dir: str = "config"):
    """Reload and return config."""
    return reload_config(config_dir)


# ── Main CLI group ──────────────────────────────────────────────

@click.group()
@click.option("--config-dir", default="config", help="Config directory path")
@click.option("--db", "db_path", default="papers.db", help="SQLite database path")
@click.pass_context
def main(ctx, config_dir, db_path):
    """Paper Database — 文献库管理系统.

    从 DBLP/Semantic Scholar/OpenAlex 拉取论文，用本地 CLI LLM 分类，
    导出 Excel/CSV 结果。
    """
    ctx.ensure_object(dict)
    ctx.obj["config_dir"] = config_dir
    ctx.obj["db_path"] = db_path


# ── Venue subcommand ────────────────────────────────────────────

@main.group()
def venue():
    """管理会议/期刊 (Venue)."""


@venue.command("init")
@click.pass_context
def venue_init(ctx):
    """从 config/venues.yaml 初始化 venue 表."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"])

    db.init_venues_from_config(config.venues)
    console.print(f"[green]✓[/] 已初始化 {len(config.venues)} 个 venue")


@venue.command("list")
@click.pass_context
def venue_list(ctx):
    """列出所有 venue."""
    db = _get_db(ctx.obj["db_path"])
    venues = db.list_venues()

    table = RichTable(title="Venues")
    table.add_column("Key", style="cyan")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("CCF")
    table.add_column("Years")

    for v in venues:
        table.add_row(
            v["key"], v["name"], v["type"],
            v.get("ccf_rank", ""),
            f"{v['year_start']}-{v['year_end']}",
        )

    console.print(table)
    console.print(f"Total: {len(venues)} venues")


# ── Paper subcommand ────────────────────────────────────────────

@main.group()
def paper():
    """管理论文 (Paper) 元数据."""


@paper.command("fetch")
@click.option("--venue", "-v", "venue_key", default=None, help="只拉取指定 venue")
@click.option("--year", "-y", "year_filter", type=int, default=None, help="只拉取指定年份")
@click.pass_context
def paper_fetch(ctx, venue_key, year_filter):
    """从 DBLP 拉取论文列表."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"])
    fetcher = DBLPFetcher()

    venues = config.venues
    if venue_key:
        v = config.get_venue(venue_key)
        if v is None:
            console.print(f"[red]✗[/] Venue '{venue_key}' 不在配置中")
            sys.exit(1)
        venues = [v]

    total_papers = 0

    for v in venues:
        year_start = year_filter if year_filter else v.year_start
        year_end = year_filter if year_filter else v.year_end

        for year in range(year_start, year_end + 1):
            console.print(f"  拉取 [cyan]{v.key}[/] {year}...", end=" ")
            papers = fetcher.fetch_papers_by_venue_year(v, year)

            if papers:
                db.insert_papers_batch(papers, v.key)
                console.print(f"[green]{len(papers)} 篇[/]")
                total_papers += len(papers)
            else:
                console.print("[yellow]0 篇[/]")

    console.print(f"\n[green]✓[/] 总计拉取 {total_papers} 篇论文")


@paper.command("fetch-abstracts")
@click.option("--limit", "-l", default=0, help="限制获取摘要的数量 (0=全部)")
@click.pass_context
def paper_fetch_abstracts(ctx, limit):
    """从 Semantic Scholar / OpenAlex 补全摘要."""
    db = _get_db(ctx.obj["db_path"])

    papers_without = db.get_papers_without_abstract(limit=limit or 10000)
    if not papers_without:
        console.print("[green]✓[/] 所有论文已有摘要")
        return

    console.print(f"需要获取摘要: {len(papers_without)} 篇")

    s2 = SemanticScholarFetcher()
    oa = OpenAlexFetcher()

    s2_count = 0
    oa_count = 0
    failed = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("获取摘要...", total=len(papers_without))

        for row in papers_without:
            paper = PaperMeta(
                title=row["title"],
                year=row["year"],
                authors=json.loads(row["authors"]),
                dblp_key=row["dblp_key"],
                doi=row.get("doi", "") or "",
            )

            # Try Semantic Scholar first
            abstract = s2.fetch_abstract(paper)
            source = "semantic_scholar"
            if abstract:
                s2_count += 1
            else:
                # Fallback to OpenAlex
                abstract = oa.fetch_abstract(paper)
                source = "openalex"
                if abstract:
                    oa_count += 1
                else:
                    failed += 1

            if abstract:
                db.update_paper_abstract(
                    row["dblp_key"], abstract, source,
                    citation_count=paper.citation_count,
                    doi=paper.doi,
                )

            progress.update(task, advance=1)

    console.print(
        f"[green]✓[/] S2: {s2_count} | OpenAlex: {oa_count} | "
        f"Failed: {failed}"
    )


@paper.command("fetch-all")
@click.option("--venue", "-v", "venue_key", default=None, help="只拉取指定 venue")
@click.option("--year", "-y", "year_filter", type=int, default=None, help="只拉取指定年份")
@click.pass_context
def paper_fetch_all(ctx, venue_key, year_filter):
    """一键拉取论文列表 + 摘要."""
    # First, fetch paper list
    ctx.invoke(paper_fetch, venue_key=venue_key, year_filter=year_filter)
    # Then, fetch abstracts
    ctx.invoke(paper_fetch_abstracts)


@paper.command("stats")
@click.pass_context
def paper_stats(ctx):
    """查看论文统计."""
    db = _get_db(ctx.obj["db_path"])
    stats = db.paper_stats()

    console.print(f"\n[bold]论文统计[/]")
    console.print(f"  总计: {stats['total']} 篇")
    console.print(f"  有摘要: {stats['with_abstract']} 篇")

    table = RichTable(title="按 Venue + Year 统计")
    table.add_column("Venue", style="cyan")
    table.add_column("Year")
    table.add_column("Count")

    for row in stats["by_venue_year"]:
        table.add_row(row["venue_key"], str(row["year"]), str(row["cnt"]))

    console.print(table)


# ── Survey subcommand ───────────────────────────────────────────

@main.group()
def survey():
    """管理调研 (Survey)."""


@survey.command("create")
@click.option("--topic", "-t", required=True, help="调研主题 key (对应 config/topics.yaml)")
@click.option("--name", "-n", default="", help="调研名称 (默认自动生成)")
@click.option("--venue-filter", default="", help="只包含指定 venue (逗号分隔)")
@click.option("--year-filter", default="", help="年份范围, 如 2020-2026")
@click.pass_context
def survey_create(ctx, topic, name, venue_filter, year_filter):
    """创建新调研."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"])

    topic_cfg = config.get_topic(topic)
    if topic_cfg is None:
        console.print(f"[red]✗[/] Topic '{topic}' 不在配置中。可用: "
                      f"{[t.key for t in config.topics]}")
        sys.exit(1)

    # Parse venue filter
    vf = [v.strip() for v in venue_filter.split(",") if v.strip()] or None

    # Parse year filter
    yf = None
    if year_filter:
        parts = year_filter.split("-")
        if len(parts) == 2:
            yf = (int(parts[0]), int(parts[1]))

    survey_id = db.create_survey(
        topic_cfg,
        name=name,
        cli_tool=config.classifier.tool,
        venue_filter=vf,
        year_filter=yf,
    )

    stats = db.survey_stats(survey_id)
    console.print(f"[green]✓[/] 创建调研 #{survey_id}: {topic_cfg.name}")
    console.print(f"   待分类论文: {stats['total']} 篇")


@survey.command("list")
@click.pass_context
def survey_list(ctx):
    """列出所有调研."""
    db = _get_db(ctx.obj["db_path"])
    surveys = db.list_surveys()

    table = RichTable(title="Surveys")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Topic")
    table.add_column("CLI Tool")
    table.add_column("Status")
    table.add_column("Created")

    for s in surveys:
        table.add_row(
            str(s["id"]), s["name"], s["topic_key"],
            s.get("cli_tool", ""), s.get("status", ""),
            s.get("created_at", "")[:19],
        )

    console.print(table)


@survey.command("stats")
@click.option("--survey-id", "-s", type=int, required=True)
@click.pass_context
def survey_stats(ctx, survey_id):
    """查看调研进度."""
    db = _get_db(ctx.obj["db_path"])
    s = db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    stats = db.survey_stats(survey_id)

    console.print(f"\n[bold]Survey #{survey_id}[/]: {s['name']}")
    console.print(f"  Topic: {s['topic_key']}")
    console.print(f"  Status: {s.get('status', 'pending')}")
    console.print(f"  总论文数: {stats['total']}")
    console.print(f"  已分类: {stats['classified']} ({stats['progress_pct']}%)")
    console.print(f"  未分类: {stats['unclassified']}")
    console.print(f"  [green]相关: {stats['relevant']}[/]")
    console.print(f"  [red]不相关: {stats['not_relevant']}[/]")


@survey.command("delete")
@click.option("--survey-id", "-s", type=int, required=True)
@click.confirmation_option(prompt="确认删除此调研及所有分类结果?")
@click.pass_context
def survey_delete(ctx, survey_id):
    """删除调研."""
    db = _get_db(ctx.obj["db_path"])
    db.delete_survey(survey_id)
    console.print(f"[green]✓[/] 已删除 Survey #{survey_id}")


@survey.command("classify")
@click.option("--survey-id", "-s", type=int, required=True)
@click.option("--dry-run", is_flag=True, default=False, help="只打印 prompt，不实际调 CLI")
@click.option("--limit", "-l", type=int, default=None, help="最大分类数量")
@click.option("--start", type=int, default=1, help="从第 N 篇开始 (断点续传)")
@click.pass_context
def survey_classify(ctx, survey_id, dry_run, limit, start):
    """运行分类 (subprocess 调本地 CLI LLM)."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"])

    s = db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    topic_cfg = config.get_topic(s["topic_key"])
    if topic_cfg is None:
        console.print(f"[red]✗[/] Topic '{s['topic_key']}' 配置不存在")
        sys.exit(1)

    classifier = CLIClassifier(config.classifier)

    if dry_run:
        console.print("[yellow]DRY RUN 模式 — 只打印 prompt，不调 CLI[/]\n")

    stats = db.survey_stats(survey_id)
    console.print(f"Survey #{survey_id}: {stats['unclassified']} 篇待分类")

    def progress_callback(done, _total, title, result):
        status = "[green]✓相关[/]" if result.is_relevant else "[dim]✗不相关[/]"
        console.print(f"  [{done}] {status} {title[:70]}...")

    classifier.run_survey(
        db, survey_id, topic_cfg,
        dry_run=dry_run,
        limit=limit,
        start=start,
        progress_callback=progress_callback,
    )

    # Show final stats
    final_stats = db.survey_stats(survey_id)
    console.print(f"\n[green]✓[/] 完成! 相关: {final_stats['relevant']} / "
                  f"已分类: {final_stats['classified']}")


@survey.command("preview")
@click.option("--survey-id", "-s", type=int, required=True)
@click.option("--relevant-only", is_flag=True, default=False)
@click.option("--limit", "-l", type=int, default=20)
@click.pass_context
def survey_preview(ctx, survey_id, relevant_only, limit):
    """终端预览分类结果."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"])

    s = db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    topic_cfg = config.get_topic(s["topic_key"])
    if topic_cfg is None:
        console.print(f"[red]✗[/] Topic 配置不存在")
        sys.exit(1)

    exporter = Exporter(db)
    exporter.preview(survey_id, topic_cfg, relevant_only=relevant_only, limit=limit)


@survey.command("export")
@click.option("--survey-id", "-s", type=int, required=True)
@click.option("--output", "-o", default="results/survey", help="输出文件路径 (不含扩展名)")
@click.option("--relevant-only", is_flag=True, default=False, help="只导出相关论文")
@click.pass_context
def survey_export(ctx, survey_id, output, relevant_only):
    """导出结果到 Excel/CSV."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"])

    s = db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    topic_cfg = config.get_topic(s["topic_key"])
    if topic_cfg is None:
        console.print(f"[red]✗[/] Topic 配置不存在")
        sys.exit(1)

    # Ensure output directory exists
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    exporter = Exporter(db)
    filepath = exporter.export(
        survey_id, topic_cfg, output_path, relevant_only=relevant_only
    )
    console.print(f"[green]✓[/] 导出完成: {filepath}")


# ── Entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    main()
