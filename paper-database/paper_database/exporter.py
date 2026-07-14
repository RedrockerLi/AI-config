"""Exporter: CSV output according to topic output.columns config.

All output fields are real DB columns — venue_* from venue table,
paper_* from paper table, everything else from survey_result.
No JSON parsing needed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from paper_database.config import OutputColumn, TopicConfig
from paper_database.db import Database


class Exporter:
    """Export survey results to CSV."""

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
