"""SQLite database layer: schema creation and all CRUD operations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from paper_database.config import TopicConfig, VenueConfig
from paper_database.fetcher.base import PaperMeta


# ── Schema ──────────────────────────────────────────────────────

PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS venue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    ccf_rank TEXT DEFAULT '',
    dblp_url_prefix TEXT NOT NULL,
    year_start INTEGER NOT NULL,
    year_end INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dblp_key TEXT UNIQUE,
    title TEXT NOT NULL,
    year INTEGER NOT NULL,
    venue_id INTEGER REFERENCES venue(id),
    authors TEXT DEFAULT '[]',
    doi TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    abstract_source TEXT DEFAULT '',
    citation_count INTEGER DEFAULT 0,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    abstract_fetched_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_paper_venue_year ON paper(venue_id, year);
CREATE INDEX IF NOT EXISTS idx_paper_dblp_key ON paper(dblp_key);
"""

SURVEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS venue (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    ccf_rank TEXT DEFAULT '',
    dblp_url_prefix TEXT NOT NULL,
    year_start INTEGER NOT NULL,
    year_end INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper (
    id INTEGER PRIMARY KEY,
    dblp_key TEXT UNIQUE,
    title TEXT NOT NULL,
    year INTEGER NOT NULL,
    venue_id INTEGER REFERENCES venue(id),
    authors TEXT DEFAULT '[]',
    doi TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    abstract_source TEXT DEFAULT '',
    citation_count INTEGER DEFAULT 0,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    abstract_fetched_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_paper_venue_year ON paper(venue_id, year);
CREATE INDEX IF NOT EXISTS idx_paper_dblp_key ON paper(dblp_key);

CREATE TABLE IF NOT EXISTS survey (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    topic_snapshot TEXT DEFAULT '',
    cli_tool TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS survey_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id INTEGER REFERENCES survey(id) ON DELETE CASCADE,
    paper_id INTEGER REFERENCES paper(id),
    is_relevant INTEGER DEFAULT NULL,
    relevance_reason TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    analysis_json TEXT DEFAULT '',
    classified_at TIMESTAMP,
    UNIQUE(survey_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_sr_survey ON survey_result(survey_id);
CREATE INDEX IF NOT EXISTS idx_sr_unclassified
    ON survey_result(survey_id) WHERE is_relevant IS NULL;
"""


# ── Result dataclass ────────────────────────────────────────────

@dataclass
class SurveyResultRow:
    """A row joining paper + survey_result for preview/export."""
    paper_id: int
    title: str
    authors: str
    year: int
    venue_name: str
    doi: str
    abstract: str
    citation_count: int
    priority: str = ""            # "P1" / "P2" / "P3" / ""
    relevance_reason: str = ""
    confidence: float = 0.0
    # Structured extraction fields
    research_object: str = ""
    problem_goal: str = ""
    method_innovation: str = ""
    algorithm: str = ""


# ── Database class ──────────────────────────────────────────────

class Database:
    """SQLite database manager for paper survey."""

    def __init__(self, db_path: str | Path = "papers.db"):
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Init ─────────────────────────────────────────────────

    def init_db(self):
        """Create venue + paper tables (main database)."""
        self.conn.executescript(PAPER_SCHEMA)
        self.conn.commit()

    def init_survey_db(self):
        """Create all tables for a survey-specific database."""
        self.conn.executescript(SURVEY_SCHEMA)
        # Migrate: add analysis_json column if missing (for older survey DBs)
        try:
            self.conn.execute(
                "ALTER TABLE survey_result ADD COLUMN analysis_json TEXT DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    # ── Venue CRUD ───────────────────────────────────────────

    def upsert_venue(self, v: VenueConfig) -> bool:
        """Insert a venue if not exists. Returns True if inserted (new), False if skipped (existing)."""
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO venue (key, name, type, ccf_rank,
               dblp_url_prefix, year_start, year_end)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (v.key, v.name, v.type, v.ccf_rank,
             v.dblp_url_prefix, v.year_start, v.year_end),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_venue_id(self, key: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM venue WHERE key = ?", (key,)
        ).fetchone()
        return row["id"] if row else None

    def get_venue(self, key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM venue WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def list_venues(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM venue ORDER BY ccf_rank, type, key"
        ).fetchall()
        return [dict(r) for r in rows]

    def init_venues_from_config(self, venues: list[VenueConfig]) -> tuple[int, int]:
        """Initialize venue table from config list. Safe to run repeatedly.

        Returns (new_count, existing_count).
        """
        new = 0
        for v in venues:
            if self.upsert_venue(v):
                new += 1
        return new, len(venues) - new

    # ── Paper CRUD ───────────────────────────────────────────

    def insert_paper(self, paper: PaperMeta, venue_id: int):
        """Insert a paper. Skip if dblp_key already exists."""
        self.conn.execute(
            """INSERT OR IGNORE INTO paper
               (dblp_key, title, year, venue_id, authors, doi)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                paper.dblp_key,
                paper.title,
                paper.year,
                venue_id,
                json.dumps(paper.authors, ensure_ascii=False),
                paper.doi,
            ),
        )
        self.conn.commit()

    def insert_papers_batch(self, papers: list[PaperMeta], venue_key: str):
        """Insert many papers for a venue. Uses executemany for speed."""
        venue_id = self.get_venue_id(venue_key)
        if venue_id is None:
            raise ValueError(f"Venue '{venue_key}' not in DB. Run 'venue init' first.")

        rows = [
            (p.dblp_key, p.title, p.year, venue_id,
             json.dumps(p.authors, ensure_ascii=False), p.doi)
            for p in papers
        ]
        self.conn.executemany(
            """INSERT OR IGNORE INTO paper
               (dblp_key, title, year, venue_id, authors, doi)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()

    def update_paper_abstract(
        self, dblp_key: str, abstract: str, source: str,
        citation_count: int = 0, doi: str = ""
    ):
        """Update abstract + metadata for a paper."""
        parts = ["abstract = ?", "abstract_source = ?", "abstract_fetched_at = ?"]
        params: list = [abstract, source, datetime.now(timezone.utc).isoformat()]

        if citation_count:
            parts.append("citation_count = ?")
            params.append(citation_count)
        if doi:
            parts.append("doi = ?")
            params.append(doi)

        params.append(dblp_key)
        self.conn.execute(
            f"UPDATE paper SET {', '.join(parts)} WHERE dblp_key = ?",
            params,
        )
        self.conn.commit()

    def get_paper_by_dblp_key(self, dblp_key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM paper WHERE dblp_key = ?", (dblp_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_papers_without_abstract(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM paper
               WHERE (abstract = '' OR abstract IS NULL)
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_papers(self, venue_key: Optional[str] = None) -> int:
        if venue_key:
            row = self.conn.execute(
                """SELECT COUNT(*) as cnt FROM paper
                   JOIN venue ON paper.venue_id = venue.id
                   WHERE venue.key = ?""",
                (venue_key,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) as cnt FROM paper").fetchone()
        return row["cnt"] if row else 0

    def count_papers_with_abstract(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM paper WHERE abstract != '' AND abstract IS NOT NULL"
        ).fetchone()
        return row["cnt"] if row else 0

    def paper_stats(self) -> dict:
        """Return paper statistics grouped by venue."""
        rows = self.conn.execute(
            """SELECT v.key as venue_key, v.name as venue_name,
                      p.year, COUNT(*) as cnt
               FROM paper p JOIN venue v ON p.venue_id = v.id
               GROUP BY v.key, p.year
               ORDER BY v.key, p.year"""
        ).fetchall()
        total = self.count_papers()
        with_abstract = self.count_papers_with_abstract()
        return {
            "total": total,
            "with_abstract": with_abstract,
            "by_venue_year": [dict(r) for r in rows],
        }

    # ── Survey DB creation (on main Database) ────────────────

    @staticmethod
    def get_next_survey_id(db_path: str | Path) -> int:
        """Scan surveys/ directory and return next available survey ID."""
        surveys_dir = Path(db_path).parent / "surveys"
        if not surveys_dir.exists():
            return 1
        ids = []
        for f in surveys_dir.glob("survey_*.db"):
            try:
                ids.append(int(f.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return max(ids, default=0) + 1

    @staticmethod
    def list_surveys_from_directory(db_path: str | Path) -> list[dict]:
        """Scan surveys/ directory and read metadata from all survey DBs."""
        surveys_dir = Path(db_path).parent / "surveys"
        if not surveys_dir.exists():
            return []

        surveys = []
        for f in sorted(surveys_dir.glob("survey_*.db"), reverse=True):
            try:
                sdb = Database(str(f))
                rows = sdb.conn.execute(
                    "SELECT * FROM survey ORDER BY created_at DESC"
                ).fetchall()
                for r in rows:
                    surveys.append(dict(r))
            except Exception:
                continue
        return surveys

    def create_survey_db(
        self,
        survey_id: int,
        name: str,
        topic: TopicConfig,
        cli_tool: str = "",
        venue_filter: Optional[list[str]] = None,
        year_filter: Optional[tuple[int, int]] = None,
    ) -> "Database":
        """Create a survey database file with snapshot of matching papers.

        Returns the survey Database instance (already initialized).
        """
        papers_dir = Path(self.db_path).parent
        surveys_dir = papers_dir / "surveys"
        surveys_dir.mkdir(parents=True, exist_ok=True)
        survey_db_path = surveys_dir / f"survey_{survey_id}.db"

        # Create and initialize the survey DB
        survey_db = Database(str(survey_db_path))
        survey_db.init_survey_db()

        # Build WHERE clause for paper matching
        where_clauses = ["1=1"]
        params: list = []

        if venue_filter:
            placeholders = ",".join("?" * len(venue_filter))
            where_clauses.append(
                f"p.venue_id IN (SELECT id FROM venue WHERE key IN ({placeholders}))"
            )
            params.extend(venue_filter)

        if year_filter:
            where_clauses.append("p.year >= ? AND p.year <= ?")
            params.extend([year_filter[0], year_filter[1]])

        where_sql = " AND ".join(where_clauses)

        # Get matching paper rows
        paper_rows = self.conn.execute(
            f"""SELECT p.* FROM paper p WHERE {where_sql}""",
            params,
        ).fetchall()

        if not paper_rows:
            raise ValueError("No papers match the survey criteria")

        # Collect unique venue IDs
        venue_ids = set(row["venue_id"] for row in paper_rows)

        # Copy venue records (preserving IDs)
        for vid in venue_ids:
            vrow = self.conn.execute(
                "SELECT * FROM venue WHERE id = ?", (vid,)
            ).fetchone()
            if vrow:
                survey_db.conn.execute(
                    """INSERT INTO venue (id, key, name, type, ccf_rank,
                       dblp_url_prefix, year_start, year_end)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (vrow["id"], vrow["key"], vrow["name"], vrow["type"],
                     vrow["ccf_rank"], vrow["dblp_url_prefix"],
                     vrow["year_start"], vrow["year_end"]),
                )

        # Copy paper records (preserving IDs)
        for row in paper_rows:
            survey_db.conn.execute(
                """INSERT INTO paper (id, dblp_key, title, year, venue_id,
                   authors, doi, abstract, abstract_source, citation_count,
                   fetched_at, abstract_fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["id"], row["dblp_key"], row["title"], row["year"],
                 row["venue_id"], row["authors"], row["doi"],
                 row["abstract"], row["abstract_source"],
                 row["citation_count"], row["fetched_at"],
                 row["abstract_fetched_at"]),
            )

        # Topic snapshot
        topic_snapshot = json.dumps({
            "key": topic.key,
            "name": topic.name,
            "description": topic.description,
            "keywords": topic.keywords,
        }, ensure_ascii=False)

        # Insert survey row with explicit ID
        survey_db.conn.execute(
            """INSERT INTO survey (id, name, topic_key, topic_snapshot,
               cli_tool, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (survey_id, name, topic.key, topic_snapshot, cli_tool,
             datetime.now(timezone.utc).isoformat()),
        )

        # Populate survey_result (one row per copied paper)
        survey_db.conn.executemany(
            "INSERT INTO survey_result (survey_id, paper_id) VALUES (?, ?)",
            [(survey_id, row["id"]) for row in paper_rows],
        )

        survey_db.conn.commit()
        return survey_db

    # ── Survey CRUD (works on any Database, main or survey) ───

    def get_survey(self, survey_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM survey WHERE id = ?", (survey_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_surveys(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM survey ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_survey(self, survey_id: int):
        self.conn.execute("DELETE FROM survey WHERE id = ?", (survey_id,))
        self.conn.commit()

    def reset_survey(self, survey_id: int):
        """Clear classification results but keep the survey and paper data."""
        self.conn.execute(
            """UPDATE survey_result
               SET is_relevant = NULL, relevance_reason = '', confidence = 0.0,
                   analysis_json = '', classified_at = NULL
               WHERE survey_id = ?""",
            (survey_id,),
        )
        self.conn.commit()

    def survey_stats(self, survey_id: int) -> dict:
        """Return classification progress stats with P1/P2/P3 breakdown."""
        total_row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM survey_result WHERE survey_id = ?",
            (survey_id,),
        ).fetchone()
        classified_row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM survey_result
               WHERE survey_id = ? AND is_relevant IS NOT NULL
               AND is_relevant != ''""",
            (survey_id,),
        ).fetchone()
        p1_row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM survey_result
               WHERE survey_id = ? AND is_relevant = 'P1'""",
            (survey_id,),
        ).fetchone()
        p2_row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM survey_result
               WHERE survey_id = ? AND is_relevant = 'P2'""",
            (survey_id,),
        ).fetchone()
        p3_row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM survey_result
               WHERE survey_id = ? AND is_relevant = 'P3'""",
            (survey_id,),
        ).fetchone()

        total = total_row["cnt"] if total_row else 0
        classified = classified_row["cnt"] if classified_row else 0
        p1 = p1_row["cnt"] if p1_row else 0
        p2 = p2_row["cnt"] if p2_row else 0
        p3 = p3_row["cnt"] if p3_row else 0

        return {
            "survey_id": survey_id,
            "total": total,
            "classified": classified,
            "unclassified": total - classified,
            "p1": p1,
            "p2": p2,
            "p3": p3,
            "relevant": p1 + p2 + p3,
            "not_relevant": classified - p1 - p2 - p3,
            "progress_pct": round(classified / total * 100, 1) if total > 0 else 0,
        }

    def get_unclassified(
        self, survey_id: int, limit: int = 50
    ) -> list[dict]:
        """Get unclassified survey_result rows with joined paper data."""
        rows = self.conn.execute(
            """SELECT sr.id as result_id, sr.paper_id,
                      p.title, p.year, p.authors, p.doi, p.abstract,
                      p.citation_count, p.dblp_key,
                      v.name as venue_name, v.key as venue_key, v.ccf_rank
               FROM survey_result sr
               JOIN paper p ON sr.paper_id = p.id
               JOIN venue v ON p.venue_id = v.id
               WHERE sr.survey_id = ? AND sr.is_relevant IS NULL
               ORDER BY v.ccf_rank, p.year DESC, p.title
               LIMIT ?""",
            (survey_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_result(
        self, result_id: int, is_relevant: str = "",
        reason: str = "", confidence: float = 0.0,
        analysis_json: str = "",
    ):
        """Mark a single survey_result as classified. is_relevant stores priority ("P1"/"P2"/"P3"/"")."""
        self.conn.execute(
            """UPDATE survey_result
               SET is_relevant = ?, relevance_reason = ?, confidence = ?,
                   analysis_json = ?, classified_at = ?
               WHERE id = ?""",
            (is_relevant, reason, confidence,
             analysis_json,
             datetime.now(timezone.utc).isoformat(), result_id),
        )
        self.conn.commit()

    def mark_batch(self, results: list[dict]):
        """Batch mark survey_results. Each dict: {id, is_relevant, reason, confidence, analysis_json}.
        is_relevant stores priority string ("P1"/"P2"/"P3"/"")."""
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (r.get("is_relevant", ""),
             r.get("reason", ""),
             r.get("confidence", 0.0),
             r.get("analysis_json", ""),
             now,
             r["id"])
            for r in results
        ]
        self.conn.executemany(
            """UPDATE survey_result
               SET is_relevant = ?, relevance_reason = ?, confidence = ?,
                   analysis_json = ?, classified_at = ?
               WHERE id = ?""",
            rows,
        )
        self.conn.commit()

    def get_survey_results(
        self, survey_id: int, relevant_only: bool = False
    ) -> list[SurveyResultRow]:
        """Get all classified results for a survey, joined with paper + venue."""
        where = "sr.survey_id = ?"
        if relevant_only:
            where += " AND sr.is_relevant != ''"

        rows = self.conn.execute(
            f"""SELECT sr.paper_id, p.title, p.authors, p.year,
                       v.name as venue_name, p.doi, p.abstract,
                       p.citation_count, sr.is_relevant,
                       sr.relevance_reason, sr.confidence,
                       sr.analysis_json
               FROM survey_result sr
               JOIN paper p ON sr.paper_id = p.id
               JOIN venue v ON p.venue_id = v.id
               WHERE {where}
               ORDER BY v.ccf_rank, p.year DESC, p.title""",
            (survey_id,),
        ).fetchall()

        results = []
        for r in rows:
            # Parse structured extraction from analysis_json
            analysis = {}
            raw = r["analysis_json"] or ""
            if raw:
                try:
                    analysis = json.loads(raw)
                except json.JSONDecodeError:
                    pass

            priority = (r["is_relevant"] or "").strip()
            if priority not in ("P1", "P2", "P3"):
                priority = ""

            results.append(SurveyResultRow(
                paper_id=r["paper_id"],
                title=r["title"],
                authors=r["authors"],
                year=r["year"],
                venue_name=r["venue_name"],
                doi=r["doi"],
                abstract=r["abstract"],
                citation_count=r["citation_count"],
                priority=priority,
                relevance_reason=r["relevance_reason"] or "",
                confidence=r["confidence"] or 0.0,
                research_object=analysis.get("research_object", "") or analysis.get("研究对象", ""),
                problem_goal=analysis.get("problem_goal", "") or analysis.get("问题/目标", ""),
                method_innovation=analysis.get("method_innovation", "") or analysis.get("方法/创新", ""),
                algorithm=analysis.get("algorithm", "") or analysis.get("调度算法", ""),
            ))
        return results
