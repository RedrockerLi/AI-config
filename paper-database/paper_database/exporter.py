"""Exporter: CSV and Markdown output according to topic output.columns config.

All output fields are real DB columns — venue_* from venue table,
paper_* from paper table, everything else from survey_result.
No JSON parsing needed.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from paper_database.config import OutputColumn, TopicConfig
from paper_database.db import Database


class Exporter:
    """Export survey results to CSV and Markdown."""

    def __init__(self, db: Database):
        self.db = db

    def export(
        self,
        survey_id: int,
        topic: TopicConfig,
        output_path: str | Path,
    ) -> Path:
        """Export survey results to CSV (include=1 papers only)."""
        output_path = Path(output_path)
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")

        output_cfg = topic.output
        rows = self.db.get_survey_results(survey_id)

        if not rows:
            print("No results to export.")
            return output_path

        if output_cfg.sort_by:
            rows = self._sort_rows(rows, output_cfg.sort_by)

        self._write_csv(output_path, rows, output_cfg.columns)
        print(f"Exported {len(rows)} rows → {output_path}")
        return output_path

    def export_markdown(
        self,
        survey_id: int,
        topic: TopicConfig,
        output_dir: str | Path,
    ) -> list[Path]:
        """Export survey results to Markdown files, one per venue.

        Structure:
          # Venue Name
          ## Year
          ### Paper Title
          - **Field**: value
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        output_cfg = topic.output
        rows = self.db.get_survey_results(survey_id)

        if not rows:
            print("No results to export.")
            return []

        # Columns with rank: used for sorting within each year
        ranked_cols = [c for c in output_cfg.columns if c.rank]

        # Group by venue_key (preserve venue_name from first paper of each venue)
        venues: dict[str, dict] = {}
        for row in rows:
            vk = row.get("venue_key", "unknown")
            if vk not in venues:
                venues[vk] = {
                    "name": row.get("venue_name", vk),
                    "papers": [],
                }
            venues[vk]["papers"].append(row)

        generated: list[Path] = []

        for venue_key, venue_data in venues.items():
            venue_name = venue_data["name"]
            papers = venue_data["papers"]

            # Group by year
            by_year: dict[int, list[dict]] = {}
            for p in papers:
                y = p.get("paper_year", 0)
                by_year.setdefault(y, []).append(p)

            safe_name = _sanitize_filename(venue_name)
            filepath = output_dir / f"{safe_name}.md"

            lines: list[str] = []
            lines.append(f"# {venue_name}")
            lines.append("")

            for year in sorted(by_year.keys(), reverse=True):
                year_papers = by_year[year]

                # Sort: rank fields first, then title as tiebreaker
                if ranked_cols:
                    year_papers = self._sort_rows_by_rank(year_papers, ranked_cols)
                else:
                    year_papers = sorted(
                        year_papers, key=lambda r: r.get("paper_title", "")
                    )

                lines.append(f"## {year}")
                lines.append("")

                for p in year_papers:
                    title = p.get("paper_title", "Untitled")
                    lines.append(f"### {title}")
                    lines.append("")

                    for col in output_cfg.columns:
                        value = self._get_cell_value(p, col)
                        if not value:
                            continue
                        if col.field == "paper_doi":
                            # Value may be a bare DOI or already a full URL
                            href = value if value.startswith("http") else f"https://doi.org/{value}"
                            lines.append(
                                f"- **{col.header}**: [{value}]({href})"
                            )
                        else:
                            lines.append(f"- **{col.header}**: {value}")

                    lines.append("")

            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            generated.append(filepath)
            print(f"Exported {len(papers)} papers → {filepath}")

        return generated

    def preview(
        self,
        survey_id: int,
        topic: TopicConfig,
        limit: int = 20,
    ):
        """Rich table preview in terminal (include=1 papers only)."""
        from rich.console import Console
        from rich.table import Table

        rows = self.db.get_survey_results(survey_id)
        output_cfg = topic.output

        if output_cfg.sort_by:
            rows = self._sort_rows(rows, output_cfg.sort_by)

        if limit:
            rows = rows[:limit]

        console = Console()
        table = Table(title=f"Survey Preview — {topic.name}")

        for col in output_cfg.columns:
            table.add_column(col.header, width=min(col.width, 40), no_wrap=False)

        for row in rows:
            values = [self._get_cell_value(row, col) for col in output_cfg.columns]
            table.add_row(*values)

        console.print(table)
        console.print(f"\nShowing {len(rows)} results.")

    # ── Internal ─────────────────────────────────────────────

    @staticmethod
    def _sort_rows(rows: list[dict], sort_by: list[str]) -> list[dict]:
        def sort_key_desc(row: dict):
            keys = []
            for field in sort_by:
                val = row.get(field, "")
                if field in ("year", "paper_year"):
                    try:
                        keys.append(-int(val))
                    except (ValueError, TypeError):
                        keys.append(0)
                else:
                    keys.append(str(val) if val is not None else "")
            return tuple(keys)

        return sorted(rows, key=sort_key_desc)

    @staticmethod
    def _sort_rows_by_rank(
        rows: list[dict], ranked_cols: list[OutputColumn]
    ) -> list[dict]:
        """Sort rows by rank fields. Each OutputColumn.rank is an ordered list;
        values appearing earlier in the list sort first. Values not in the
        list sort last. Multiple ranked columns produce multi-level sort
        (first column is primary key, etc.).
        """
        # Build {col.field: {value: position}} lookup
        rank_maps: dict[str, dict[str, int]] = {}
        for col in ranked_cols:
            rank_maps[col.field] = {
                v: i for i, v in enumerate(col.rank)
            }

        def sort_key(row: dict):
            keys = []
            for col in ranked_cols:
                val = row.get(col.field, "")
                val_str = str(val) if val is not None else ""
                # Position in rank list; values not in list get a large sentinel
                pos = rank_maps[col.field].get(val_str, len(col.rank))
                keys.append(pos)
            # Tiebreaker: paper title
            keys.append(row.get("paper_title", ""))
            return tuple(keys)

        return sorted(rows, key=sort_key)

    @staticmethod
    def _get_cell_value(row: dict, col: OutputColumn) -> str:
        # Direct key in row dict — all columns are now real DB columns
        val = row.get(col.field, "")

        # Apply transforms
        if col.transform == "join_comma":
            try:
                authors = json.loads(str(val))
                return ", ".join(authors)
            except (json.JSONDecodeError, TypeError):
                return str(val) if val else ""

        elif col.transform == "bool_to_yes_no":
            if val is True or val == 1:
                return "是"
            elif val is False or val == 0:
                return "否"
            return ""

        elif col.transform == "percent":
            try:
                return f"{float(val) * 100:.0f}%"
            except (ValueError, TypeError):
                return str(val) if val else ""

        return str(val) if val is not None else ""

    @staticmethod
    def _write_csv(
        filepath: Path,
        rows: list[dict],
        columns: list[OutputColumn],
    ):
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([col.header for col in columns])
            for row in rows:
                writer.writerow(
                    [Exporter._get_cell_value(row, col) for col in columns]
                )


def _sanitize_filename(name: str) -> str:
    """Replace characters invalid in filenames with underscore."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)
