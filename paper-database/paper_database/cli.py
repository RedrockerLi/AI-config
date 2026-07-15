"""CLI entry point for paper-database framework.

Usage:
    python -m paper_database venue init
    python -m paper_database paper fetch-all
    python -m paper_database survey create --topic scheduling
    python -m paper_database survey classify --survey-id 1
    python -m paper_database survey export --survey-id 1
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table as RichTable

from paper_database.classifier import LLMClassifier
from paper_database.config import get_config, reload_config, TopicConfig
from paper_database.db import Database
from paper_database.exporter import Exporter
from paper_database.fetcher.base import PaperMeta
from paper_database.fetcher.dblp import DBLPFetcher
from paper_database.fetcher.openalex import OpenAlexFetcher
from paper_database.fetcher.semantic_scholar import SemanticScholarFetcher

console = Console()


# ── Helpers ─────────────────────────────────────────────────────

def _get_db(db_path: str = "papers.db", config_dir: str = "config") -> Database:
    """Get a Database instance and ensure tables exist.

    Relative db_path is resolved against the project root (parent of config_dir),
    so the DB is always in the project directory regardless of CWD.
    """
    p = Path(db_path)
    if not p.is_absolute():
        p = Path(config_dir).resolve().parent / p
    db = Database(str(p))
    db.init_db()
    return db


def _resolve_config(config_dir: str = "config"):
    """Reload and return config."""
    return reload_config(config_dir)


def _get_survey_db(survey_id: int, config_dir: str = "config") -> Database:
    """Open a survey-specific database by ID. Exits on not-found."""
    project_root = Path(config_dir).resolve().parent
    survey_path = project_root / "surveys" / f"survey_{survey_id}.db"
    if not survey_path.exists():
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)
    db = Database(str(survey_path))
    db.init_survey_db()
    return db


# ── Default paths (relative to this file, CWD-independent) ──────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = str(_PROJECT_ROOT / "config")
_DEFAULT_DB = str(_PROJECT_ROOT / "papers.db")


# ── Main CLI group ──────────────────────────────────────────────

@click.group()
@click.option("--config-dir", default=_DEFAULT_CONFIG, help="Config directory path")
@click.option("--db", "db_path", default=_DEFAULT_DB, help="SQLite database path")
@click.pass_context
def main(ctx, config_dir, db_path):
    """Paper Database — 文献库管理系统.

    从 DBLP/Semantic Scholar/OpenAlex 拉取论文，用 DeepSeek API 分类，
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
    """从 config/venues.yaml 同步 venue 表（只添加新 venue，不修改已有数据）."""
    config = _resolve_config(ctx.obj["config_dir"])
    db = _get_db(ctx.obj["db_path"], ctx.obj["config_dir"])

    new, existing = db.init_venues_from_config(config.venues)
    if new > 0:
        console.print(
            f"[green]✓[/] 新增 {new} 个 venue"
            + (f"，已有 {existing} 个跳过" if existing > 0 else "")
        )
    else:
        console.print(f"[dim]✓[/] 全部 {existing} 个 venue 已存在，无需添加")


@venue.command("list")
@click.pass_context
def venue_list(ctx):
    """列出所有 venue."""
    db = _get_db(ctx.obj["db_path"], ctx.obj["config_dir"])
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
    db = _get_db(ctx.obj["db_path"], ctx.obj["config_dir"])
    fetcher = DBLPFetcher()

    venues = config.venues
    if venue_key:
        v = config.get_venue(venue_key)
        if v is None:
            console.print(f"[red]✗[/] Venue '{venue_key}' 不在配置中")
            sys.exit(1)
        venues = [v]

    total_papers = 0
    skipped_years = 0

    for vi, v in enumerate(venues):
        year_start = year_filter if year_filter else v.year_start
        year_end = year_filter if year_filter else v.year_end

        # Check which years are potentially missing: either no papers in DB,
        # or multi-volume year where not all volumes were fetched.
        # First, discover URLs from index.xml to know what SHOULD exist.
        # But we can skip index.xml if ALL years have papers AND no year
        # has missing volumes in fetched_log.

        years_to_check = list(range(year_start, year_end + 1))

        # ── Fast path: all years have papers → check fetched_log ──
        # If every year has at least some papers, we might still be missing
        # volumes. But we only know after comparing with index.xml.
        # Strategy: fetch index.xml only if at least one year has 0 papers.
        all_have_papers = all(
            db.count_papers_for_venue_year(v.key, y) > 0
            for y in years_to_check
        )

        if all_have_papers and years_to_check:
            # All years have papers, but multi-volume may be incomplete.
            # Only check index.xml if there are multi-volume venues
            # (ASPLOS etc.) — for single-volume venues, count>0 is sufficient.
            # For now, still do a light check via fetched_log.
            fetched = db.get_fetched_urls(v.key)
            fully_complete = True
            for y in years_to_check:
                if y not in fetched:
                    # No fetched_log entries → old data from before this feature
                    # Trust count>0 for single-volume, but warn for known multi-vol
                    fully_complete = False
                    break
            if fully_complete:
                for y in years_to_check:
                    console.print(
                        f"  [cyan]{v.key}[/] {y}... "
                        f"[dim]已存在 {db.count_papers_for_venue_year(v.key, y)} 篇，跳过[/]"
                    )
                    skipped_years += 1
                continue

        # Small delay between venues to avoid DBLP rate limits (429)
        if vi > 0:
            time.sleep(1.0)

        # ── Discover URLs from index.xml ──
        year_urls = fetcher.discover_year_urls(v)
        index_failed = len(year_urls) == 0

        # Get previously fetched URLs for this venue
        fetched_urls = db.get_fetched_urls(v.key)

        for year in years_to_check:
            # Determine which URLs are already fetched for this year
            discovered = set(year_urls.get(year, []))
            already_fetched = fetched_urls.get(year, set())
            missing_urls = discovered - already_fetched

            # If index failed and no fetched info, for conferences try legacy
            if index_failed:
                existing = db.count_papers_for_venue_year(v.key, year)
                if existing > 0:
                    console.print(
                        f"  [cyan]{v.key}[/] {year}... "
                        f"[dim]已存在 {existing} 篇，跳过[/]"
                    )
                    skipped_years += 1
                    continue
                elif v.type == "conference":
                    # Let fetch_papers_by_venue_year try legacy URL
                    missing_urls = {"__legacy__"}
                else:
                    console.print(
                        f"  [cyan]{v.key}[/] {year}... "
                        f"[yellow]DBLP 无数据[/]"
                    )
                    continue

            # All volumes already fetched
            if discovered and not missing_urls:
                existing = db.count_papers_for_venue_year(v.key, year)
                console.print(
                    f"  [cyan]{v.key}[/] {year}... "
                    f"[dim]已存在 {existing} 篇，跳过[/]"
                )
                skipped_years += 1
                continue

            # No data on DBLP for this year
            if not discovered and not index_failed:
                console.print(
                    f"  [cyan]{v.key}[/] {year}... "
                    f"[yellow]DBLP 无数据[/]"
                )
                continue

            # ── Fetch missing volumes ──
            # Build URL list: if we have specific missing URLs, pass them;
            # if it's a legacy fallback, pass None to let the fetcher discover.
            if missing_urls == {"__legacy__"}:
                fetch_urls = None  # Fetcher will try legacy URL
            elif missing_urls:
                fetch_urls = list(missing_urls)
            else:
                fetch_urls = None  # All discovered, fetcher will use index

            n_vols = len(fetch_urls) if fetch_urls else 1
            vol_info = f" ({n_vols} 卷)" if n_vols > 1 else ""
            extra = ""
            if discovered and already_fetched:
                extra = (
                    f" [dim](已取 {len(already_fetched)}/{len(discovered)} 卷)[/] "
                )
            console.print(
                f"  拉取 [cyan]{v.key}[/] {year}{vol_info}...{extra}", end=" "
            )
            papers, fetched_now = fetcher.fetch_papers_by_venue_year(
                v, year, urls=fetch_urls
            )

            if papers:
                db.insert_papers_batch(papers, v.key)
                console.print(f"[green]{len(papers)} 篇[/]")
                total_papers += len(papers)

                # Mark each successfully fetched URL
                for url in fetched_now:
                    db.mark_url_fetched(v.key, year, url, len(papers))
            else:
                console.print("[yellow]0 篇[/]")

    summary_parts = [f"总计拉取 {total_papers} 篇论文"]
    if skipped_years > 0:
        summary_parts.append(f"跳过 {skipped_years} 个已存在的年份")
    console.print(f"\n[green]✓[/] {', '.join(summary_parts)}")


@paper.command("fetch-abstracts")
@click.option("--limit", "-l", default=0,
              help="每批获取摘要的数量 (0=默认 10000)")
@click.option("--stop-after", "--max-total", type=int, default=0,
              help="最多获取多少篇摘要后停止 (0=不限制)")
@click.option("--doi-only", is_flag=True, default=False,
              help="仅批量 DOI 查询 (10 credits/50 篇)，跳过昂贵的标题搜索")
@click.pass_context
def paper_fetch_abstracts(ctx, limit, stop_after, doi_only):
    """从 Semantic Scholar / OpenAlex 补全摘要（自动续跑，直到全部完成）."""
    db = _get_db(ctx.obj["db_path"], ctx.obj["config_dir"])
    batch_size = limit or 10000

    total = db.count_papers()
    with_abstract = db.count_papers_with_abstract()
    remaining = total - with_abstract

    if remaining == 0:
        console.print("[green]✓[/] 所有论文已有摘要")
        return

    estimated_batches = (remaining + batch_size - 1) // batch_size
    console.print(
        f"需要获取摘要: {remaining} 篇 "
        f"(预计 {estimated_batches} 批, 每批 {batch_size} 篇)"
    )

    s2 = None
    oa = OpenAlexFetcher()

    total_s2 = 0
    total_oa = 0
    total_failed = 0
    prev_remaining = remaining
    batch_num = 0

    while True:
        batch_num += 1

        papers_without = db.get_papers_without_abstract(limit=batch_size)
        if not papers_without:
            break

        # 应用 --stop-after 上限
        total_done = total_s2 + total_oa
        if stop_after > 0 and total_done >= stop_after:
            break
        if stop_after > 0 and total_done + len(papers_without) > stop_after:
            papers_without = papers_without[: stop_after - total_done]

        if batch_num > 1:
            console.print(
                f"\n[bold]--- 批次 {batch_num}/{estimated_batches}，"
                f"自动续跑... ---[/]"
            )

        # Build PaperMeta list
        all_papers = [
            PaperMeta(
                title=row["title"],
                year=row["year"],
                authors=json.loads(row["authors"]),
                dblp_key=row["dblp_key"],
                doi=row.get("doi", "") or "",
            )
            for row in papers_without
        ]

        batch_s2 = 0
        batch_oa = 0
        batch_failed = 0

        # ── Phase 1: Semantic Scholar (only with API key) ──────────
        s2_results: dict = {}
        if os.environ.get("S2_API_KEY"):
            if s2 is None:
                s2 = SemanticScholarFetcher()
            doi_count = sum(1 for p in all_papers if p.doi.strip())
            console.print(
                f"\n[bold]Phase 1: Semantic Scholar[/] "
                f"({doi_count} 篇有 DOI → batch, 其余标题搜索)"
            )
            s2_results = s2.fetch_abstracts_batch(all_papers, db=db)
            batch_s2 += len(s2_results)
            console.print(f"  S2 成功: {batch_s2} 篇")
        else:
            console.print(
                "[dim]未设置 S2_API_KEY，跳过 Semantic Scholar，直接使用 OpenAlex[/]"
            )

        # ── Phase 2: OpenAlex 兜底 (批量 DOI 查询) ─────────────
        remaining_papers = [p for p in all_papers if p.dblp_key not in s2_results]
        if remaining_papers:
            doi_count = sum(1 for p in remaining_papers if p.doi.strip())
            console.print(
                f"\n[bold]Phase 2: OpenAlex 兜底[/] ({len(remaining_papers)} 篇, "
                f"{doi_count} 篇有 DOI → 批量查询, "
                f"{len(remaining_papers) - doi_count} 篇标题搜索)"
            )

            oa_results = oa.fetch_abstracts_batch(remaining_papers, db=db, doi_only=doi_only)

            batch_oa += len(oa_results)
            batch_failed = len(remaining_papers) - len(oa_results)
            console.print(
                f"  OpenAlex 成功: {batch_oa} 篇"
                + (f", 失败: {batch_failed} 篇" if batch_failed else "")
            )

        total_s2 += batch_s2
        total_oa += batch_oa
        total_failed += batch_failed

        # 停滞检测：本批 0 获取 + 剩余数未减少 → 无法继续
        current_remaining = db.count_papers() - db.count_papers_with_abstract()
        if (
            batch_num > 1
            and current_remaining >= prev_remaining
            and batch_s2 + batch_oa == 0
        ):
            console.print(
                f"[yellow]⚠ 本批未能获取任何摘要 "
                f"({current_remaining} 篇仍未获取), 停止[/]"
            )
            break
        prev_remaining = current_remaining

    console.print(
        f"\n[green]✓[/] 全部完成! S2: {total_s2} | OpenAlex: {total_oa} | "
        f"Failed: {total_failed}"
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
    db = _get_db(ctx.obj["db_path"], ctx.obj["config_dir"])
    stats = db.paper_stats()

    console.print(f"\n[bold]论文统计[/]")
    abstract_pct = stats['with_abstract'] / max(stats['total'], 1) * 100
    console.print(
        f"  总计: {stats['total']} 篇  |  "
        f"有摘要: {stats['with_abstract']} 篇 "
        f"([green]{abstract_pct:.1f}%[/])"
    )

    table = RichTable(title="按 Venue + Year 统计 (CCF 排序)")
    table.add_column("Venue", style="cyan", width=12)
    table.add_column("CCF", width=4)
    table.add_column("Year", width=5)
    table.add_column("Total", justify="right")
    table.add_column("有摘要", justify="right")

    for row in stats["by_venue_year"]:
        ccf = row.get("ccf_rank", "")
        abs_cnt = row["with_abstract"]
        total = row["cnt"]
        abs_display = (
            f"[red]{abs_cnt}[/]" if abs_cnt < total
            else str(abs_cnt)
        )
        table.add_row(
            row["venue_key"], ccf, str(row["year"]),
            str(total), abs_display,
        )

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
    """创建新调研 (独立调查数据库)."""
    config = _resolve_config(ctx.obj["config_dir"])
    paper_db = _get_db(ctx.obj["db_path"], ctx.obj["config_dir"])

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

    # Generate survey name
    survey_name = name or f"{topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # Get next survey ID
    survey_id = Database.get_next_survey_id(ctx.obj["db_path"])

    # Create survey database (copies matching papers + venues from main DB)
    try:
        survey_db = paper_db.create_survey_db(
            survey_id, survey_name, topic_cfg,
            cli_tool=config.classifier.model,
            venue_filter=vf, year_filter=yf,
        )
    except ValueError as e:
        console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    stats = survey_db.survey_stats(survey_id)
    console.print(f"[green]✓[/] 创建调研 #{survey_id}: {topic_cfg.name}")
    console.print(f"   数据库: surveys/survey_{survey_id}.db")
    console.print(f"   待分类论文: {stats['total']} 篇")


@survey.command("list")
@click.pass_context
def survey_list(ctx):
    """列出所有调研."""
    surveys = Database.list_surveys_from_directory(ctx.obj["db_path"])

    if not surveys:
        console.print("[dim]暂无调研[/]")
        return

    table = RichTable(title="Surveys")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Topic")
    table.add_column("Model")
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
    survey_db = _get_survey_db(survey_id, ctx.obj["config_dir"])
    s = survey_db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    stats = survey_db.survey_stats(survey_id)

    console.print(f"\n[bold]Survey #{survey_id}[/]: {s['name']}")
    console.print(f"  Topic: {s['topic_key']}")
    console.print(f"  Status: {stats.get('status', 'pending')}")
    eta = stats.get("eta")
    if eta:
        console.print(f"  [bold yellow]预计剩余时间: {eta}[/]")
    console.print(f"  总论文数: {stats['total']}")
    console.print(f"  已分类: {stats['classified']} ({stats['progress_pct']}%)")
    console.print(f"  未分类: {stats['unclassified']}")
    console.print(f"  相关: {stats['relevant']}")
    console.print(f"  不相关: {stats['not_relevant']}")


@survey.command("delete")
@click.option("--survey-id", "-s", type=int, required=True)
@click.confirmation_option(prompt="确认删除此调研及所有分类结果?")
@click.pass_context
def survey_delete(ctx, survey_id):
    """删除调研 (删除对应的数据库文件)."""
    project_root = Path(ctx.obj["config_dir"]).resolve().parent
    survey_path = project_root / "surveys" / f"survey_{survey_id}.db"
    if not survey_path.exists():
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)
    survey_path.unlink()
    console.print(f"[green]✓[/] 已删除 Survey #{survey_id} (文件已删除)")


@survey.command("reset")
@click.option("--survey-id", "-s", type=int, required=True)
@click.confirmation_option(prompt="确认清空此调研的所有分类结果?")
@click.pass_context
def survey_reset(ctx, survey_id):
    """清空调研的分类结果，保留调研和论文数据."""
    survey_db = _get_survey_db(survey_id, ctx.obj["config_dir"])
    survey_db.reset_survey(survey_id)
    console.print(f"[green]✓[/] 已清空 Survey #{survey_id} 的分类结果")


@survey.command("classify")
@click.option("--survey-id", "-s", type=int, required=True)
@click.option("--dry-run", is_flag=True, default=False, help="只打印 prompt，不实际调 API")
@click.option("--limit", "-l", type=int, default=None, help="最大分类数量")
@click.option("--no-export", is_flag=True, default=False, help="不自动导出 CSV")
@click.pass_context
def survey_classify(ctx, survey_id, dry_run, limit, no_export):
    """运行分类 (LLM API 并发), 完成后自动导出 CSV.

    支持断点续传: 中断后直接重新运行相同命令即可，已分类的论文自动跳过。
    """
    config = _resolve_config(ctx.obj["config_dir"])
    survey_db = _get_survey_db(survey_id, ctx.obj["config_dir"])

    s = survey_db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    topic_cfg = config.get_topic(s["topic_key"])
    if topic_cfg is None:
        console.print(f"[red]✗[/] Topic '{s['topic_key']}' 配置不存在")
        sys.exit(1)

    classifier = LLMClassifier(config.classifier)

    if dry_run:
        console.print("[yellow]DRY RUN 模式 — 只打印 prompt，不调 API[/]\n")

    stats = survey_db.survey_stats(survey_id)
    console.print(f"Survey #{survey_id}: {stats['unclassified']} 篇待分类")
    console.print(
        f"[dim]Model: {config.classifier.model}, "
        f"Concurrency: {config.classifier.max_concurrency}[/]"
    )

    def progress_callback(done, _total, title, result):
        if result.include:
            status = "[green]✓[/]"
        else:
            status = "[dim]✗[/]"
        t = title[:70]
        if len(title) > 70:
            t += "..."
        console.print(f"  [{done}] {status} {t}")

    asyncio.run(classifier.run_survey(
        survey_db, survey_id, topic_cfg,
        dry_run=dry_run,
        limit=limit,
        progress_callback=progress_callback,
    ))

    # Show final stats
    final_stats = survey_db.survey_stats(survey_id)
    console.print(f"\n[green]✓[/] 完成! "
                  f"已分类: {final_stats['classified']} | "
                  f"相关: {final_stats['relevant']}")

    # Auto-export CSV (unless --no-export or dry-run)
    if not dry_run and not no_export:
        safe_name = s["name"].replace(" ", "_").replace("/", "_")
        output_path = Path("results") / f"survey_{survey_id}_{safe_name}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        exporter = Exporter(survey_db)
        filepath = exporter.export(survey_id, topic_cfg, output_path)
        console.print(f"[green]✓[/] CSV 已导出: {filepath}")


@survey.command("preview")
@click.option("--survey-id", "-s", type=int, required=True)
@click.option("--limit", "-l", type=int, default=20)
@click.pass_context
def survey_preview(ctx, survey_id, limit):
    """终端预览分类结果."""
    config = _resolve_config(ctx.obj["config_dir"])
    survey_db = _get_survey_db(survey_id, ctx.obj["config_dir"])

    s = survey_db.get_survey(survey_id)
    if s is None:
        console.print(f"[red]✗[/] Survey #{survey_id} 不存在")
        sys.exit(1)

    topic_cfg = config.get_topic(s["topic_key"])
    if topic_cfg is None:
        console.print(f"[red]✗[/] Topic 配置不存在")
        sys.exit(1)

    exporter = Exporter(survey_db)
    exporter.preview(survey_id, topic_cfg, limit=limit)


@survey.command("export")
@click.option("--survey-id", "-s", type=int, required=True)
@click.option("--output", "-o", default="results/survey", help="输出文件路径 (不含扩展名)")
@click.pass_context
def survey_export(ctx, survey_id, output):
    """导出结果到 CSV (仅导出 include=1 的论文)."""
    config = _resolve_config(ctx.obj["config_dir"])
    survey_db = _get_survey_db(survey_id, ctx.obj["config_dir"])

    s = survey_db.get_survey(survey_id)
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

    exporter = Exporter(survey_db)
    filepath = exporter.export(survey_id, topic_cfg, output_path)
    console.print(f"[green]✓[/] 导出完成: {filepath}")


# ── Entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    main()
