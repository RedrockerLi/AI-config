"""SQLite database layer: schema creation and all CRUD operations."""

from __future__ import annotations

import json
import sqlite3
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

CREATE TABLE IF NOT EXISTS fetched_log (
    venue_key TEXT NOT NULL,
    year INTEGER NOT NULL,
    xml_url TEXT NOT NULL PRIMARY KEY,
    paper_count INTEGER DEFAULT 0,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fetched_log_venue_year ON fetched_log(venue_key, year);

CREATE TABLE IF NOT EXISTS paper_topic (
    paper_id INTEGER NOT NULL REFERENCES paper(id),
    source TEXT NOT NULL,
    topic TEXT NOT NULL,
    score REAL,
    PRIMARY KEY (paper_id, source, topic)
);
CREATE INDEX IF NOT EXISTS idx_pt_paper ON paper_topic(paper_id);

CREATE TABLE IF NOT EXISTS paper_reference (
    paper_id INTEGER NOT NULL REFERENCES paper(id),
    referenced_title TEXT NOT NULL,
    external_id TEXT,
    source TEXT NOT NULL,
    citation_count INTEGER DEFAULT 0,
    PRIMARY KEY (paper_id, referenced_title, source)
);
CREATE INDEX IF NOT EXISTS idx_pr_paper ON paper_reference(paper_id);

CREATE TABLE IF NOT EXISTS reference_work (
    external_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'openalex',
    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
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
    abstract_fetched_at TIMESTAMP,
    flag TEXT DEFAULT 'unclaimed'
);

CREATE INDEX IF NOT EXISTS idx_paper_venue_year ON paper(venue_id, year);
CREATE INDEX IF NOT EXISTS idx_paper_dblp_key ON paper(dblp_key);
CREATE INDEX IF NOT EXISTS idx_paper_flag ON paper(flag);

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

-- survey_result is created dynamically in create_survey_db()
-- based on topic.output.columns

CREATE TABLE IF NOT EXISTS survey_progress_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id INTEGER REFERENCES survey(id) ON DELETE CASCADE,
    classified_count INTEGER NOT NULL,
    total_count INTEGER NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_spl_survey ON survey_progress_log(survey_id, recorded_at);

CREATE TABLE IF NOT EXISTS paper_topic (
    paper_id INTEGER NOT NULL REFERENCES paper(id),
    source TEXT NOT NULL,
    topic TEXT NOT NULL,
    score REAL,
    PRIMARY KEY (paper_id, source, topic)
);
CREATE INDEX IF NOT EXISTS idx_pt_paper ON paper_topic(paper_id);

CREATE TABLE IF NOT EXISTS paper_reference (
    paper_id INTEGER NOT NULL REFERENCES paper(id),
    referenced_title TEXT NOT NULL,
    external_id TEXT,
    source TEXT NOT NULL,
    citation_count INTEGER DEFAULT 0,
    PRIMARY KEY (paper_id, referenced_title, source)
);
CREATE INDEX IF NOT EXISTS idx_pr_paper ON paper_reference(paper_id);

CREATE TABLE IF NOT EXISTS reference_work (
    external_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'openalex',
    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# ── Helpers ──────────────────────────────────────────────────────

def _survey_result_columns(topic: TopicConfig) -> list[str]:
    """Return field names from output.columns that become survey_result columns.

    Excludes venue_* / paper_* prefixed fields (those come from JOINs).
    """
    cols: list[str] = []
    for c in topic.output.columns:
        f = c.field
        if f.startswith("venue_") or f.startswith("paper_"):
            continue
        if f not in cols:
            cols.append(f)
    return cols


# ── Database class ──────────────────────────────────────────────

class Database:
    """SQLite database manager for paper survey."""

    def __init__(self, db_path: str | Path = "papers.db"):
        self._db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._survey_columns: list[str] = []  # set by create_survey_db

    @property
    def db_path(self) -> str:
        """Path to the SQLite database file (for creating thread-safe copies)."""
        return self._db_path

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")  # 30s — 高并发写入不丢
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
        """Create fixed tables (venue, paper, survey, progress_log).

        survey_result is created separately by create_survey_db() based on
        the topic's output.columns.
        """
        # Migrate: add flag column to paper if missing (older survey DBs)
        try:
            self.conn.execute("ALTER TABLE paper ADD COLUMN flag TEXT DEFAULT 'unclaimed'")
        except sqlite3.OperationalError:
            pass

        self.conn.executescript(SURVEY_SCHEMA)
        self.conn.commit()

        # Load dynamic columns from existing survey_result table
        self._load_survey_columns()

    def _load_survey_columns(self):
        """Discover dynamic survey_result columns from the table schema.

        Excludes fixed columns (id, survey_id, paper_id, include, classified_at).
        """
        try:
            info = self.conn.execute("PRAGMA table_info(survey_result)").fetchall()
        except sqlite3.OperationalError:
            return
        fixed = {"id", "survey_id", "paper_id", "include", "classified_at"}
        self._survey_columns = [r["name"] for r in info if r["name"] not in fixed]

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

    def get_paper_abstract(self, dblp_key: str) -> Optional[str]:
        """Get abstract for a paper by dblp_key. Returns None if not found."""
        row = self.conn.execute(
            "SELECT abstract FROM paper WHERE dblp_key = ? AND abstract != ''",
            (dblp_key,),
        ).fetchone()
        return row["abstract"] if row else None

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

    def get_paper_id_by_dblp_key(self, dblp_key: str) -> Optional[int]:
        """Get paper.id by dblp_key. Returns None if not found."""
        row = self.conn.execute(
            "SELECT id FROM paper WHERE dblp_key = ?", (dblp_key,)
        ).fetchone()
        return row["id"] if row else None

    def save_paper_topics(
        self, paper_id: int, source: str, topics: list[dict]
    ):
        """Save topics/concepts for a paper. Each topic: {topic, score}.

        Uses INSERT OR IGNORE — safe for re-runs.
        """
        if not topics:
            return
        self.conn.executemany(
            """INSERT OR IGNORE INTO paper_topic (paper_id, source, topic, score)
               VALUES (?, ?, ?, ?)""",
            [(paper_id, source, t["topic"], t.get("score")) for t in topics],
        )
        self.conn.commit()

    def save_paper_references(
        self, paper_id: int, source: str, refs: list[dict]
    ):
        """Save references for a paper. Each ref: {title, external_id, citation_count}.

        Uses INSERT OR IGNORE — safe for re-runs.
        """
        if not refs:
            return
        self.conn.executemany(
            """INSERT OR IGNORE INTO paper_reference
               (paper_id, referenced_title, external_id, source, citation_count)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (paper_id, r["title"], r.get("external_id", ""),
                 source, r.get("citation_count", 0))
                for r in refs
            ],
        )
        self.conn.commit()

    def get_paper_topics(self, paper_id: int) -> list[str]:
        """Get topic names for a paper, ordered by score descending."""
        rows = self.conn.execute(
            """SELECT topic FROM paper_topic
               WHERE paper_id = ?
               ORDER BY score DESC""",
            (paper_id,),
        ).fetchall()
        return [r["topic"] for r in rows]

    def get_paper_references(self, paper_id: int, limit: int = 20) -> list[str]:
        """Get referenced paper titles for a paper, most-cited first.

        Resolves placeholder URLs via reference_work cache. Skips
        references that are still unresolved.
        """
        rows = self.conn.execute(
            """SELECT COALESCE(rw.title, pr.referenced_title) as title
               FROM paper_reference pr
               LEFT JOIN reference_work rw ON pr.external_id = rw.external_id
               WHERE pr.paper_id = ?
                 AND (rw.title IS NOT NULL
                      OR (pr.referenced_title != ''
                          AND pr.referenced_title NOT LIKE 'https://openalex.org/%'))
               ORDER BY pr.citation_count DESC
               LIMIT ?""",
            (paper_id, limit),
        ).fetchall()
        return [r["title"] for r in rows]

    def save_paper_reference_ids(
        self, paper_id: int, source: str, external_ids: list[str]
    ):
        """Save reference IDs (OpenAlex URLs) as placeholder rows.

        Uses the URL itself as referenced_title placeholder — later resolved
        to real title by _resolve_referenced_works(). INSERT OR IGNORE makes
        this safe for re-runs.
        """
        if not external_ids:
            return
        unique_ids = list(set(external_ids))
        self.conn.executemany(
            """INSERT OR IGNORE INTO paper_reference
               (paper_id, referenced_title, external_id, source)
               VALUES (?, ?, ?, ?)""",
            [(paper_id, rid, rid, source) for rid in unique_ids],
        )
        self.conn.commit()

    # ── reference_work cache (dedup across all papers) ─────────

    def save_reference_works(self, works: list[dict]):
        """Batch insert resolved reference works. Dedup by external_id."""
        if not works:
            return
        self.conn.executemany(
            """INSERT OR IGNORE INTO reference_work (external_id, title, source)
               VALUES (?, ?, ?)""",
            [(w["external_id"], w["title"], w.get("source", "openalex")) for w in works],
        )
        self.conn.commit()

    def get_unresolved_ref_ids(self) -> list[str]:
        """Get external_ids in paper_reference (placeholders) NOT yet cached.

        Only returns IDs not already in reference_work — avoids re-fetching.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT pr.external_id FROM paper_reference pr
               WHERE pr.referenced_title LIKE 'https://openalex.org/%'
                 AND pr.source = 'openalex'
                 AND pr.external_id NOT IN (SELECT external_id FROM reference_work)"""
        ).fetchall()
        return [r["external_id"] for r in rows]

    def resolve_paper_references(self):
        """UPDATE paper_reference from reference_work cache.

        Replaces placeholder URLs with resolved titles for all rows
        whose external_id exists in reference_work.
        """
        self.conn.execute(
            """UPDATE paper_reference SET referenced_title = (
                   SELECT rw.title FROM reference_work rw
                   WHERE rw.external_id = paper_reference.external_id
               )
               WHERE referenced_title LIKE 'https://openalex.org/%'
                 AND external_id IN (SELECT external_id FROM reference_work)"""
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

    def count_papers_for_venue_year(self, venue_key: str, year: int) -> int:
        """Return number of papers for a specific venue + year combination."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM paper
               JOIN venue ON paper.venue_id = venue.id
               WHERE venue.key = ? AND paper.year = ?""",
            (venue_key, year),
        ).fetchone()
        return row["cnt"] if row else 0

    # ── Fetched URL tracking (for resume / multi-volume) ─────

    def mark_url_fetched(
        self, venue_key: str, year: int, xml_url: str, paper_count: int = 0
    ):
        """Record that a specific DBLP XML URL was successfully downloaded."""
        self.conn.execute(
            """INSERT OR REPLACE INTO fetched_log
               (venue_key, year, xml_url, paper_count, fetched_at)
               VALUES (?, ?, ?, ?, ?)""",
            (venue_key, year, xml_url, paper_count,
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_fetched_urls(self, venue_key: str) -> dict[int, set[str]]:
        """Return {year: {xml_url, ...}} of previously fetched XML URLs."""
        rows = self.conn.execute(
            """SELECT year, xml_url FROM fetched_log
               WHERE venue_key = ?""",
            (venue_key,),
        ).fetchall()
        result: dict[int, set[str]] = {}
        for r in rows:
            year = r["year"]
            if year not in result:
                result[year] = set()
            result[year].add(r["xml_url"])
        return result

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

    def count_papers_without_topics(self) -> int:
        """Count papers that have no topic entries."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM paper
               WHERE id NOT IN (SELECT DISTINCT paper_id FROM paper_topic)"""
        ).fetchone()
        return row["cnt"] if row else 0

    def count_papers_without_references(self) -> int:
        """Count papers that have no reference entries."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM paper
               WHERE id NOT IN (SELECT DISTINCT paper_id FROM paper_reference)"""
        ).fetchone()
        return row["cnt"] if row else 0

    def get_papers_needing_enrichment(self, limit: int = 500) -> list[dict]:
        """Get papers missing ANY of: abstract, topics, or references.

        Returns papers sorted by need: totally empty first, then partial.
        """
        rows = self.conn.execute(
            """SELECT p.*,
                      (CASE WHEN p.abstract = '' OR p.abstract IS NULL THEN 1 ELSE 0 END) as need_abstract,
                      (CASE WHEN pt.paper_id IS NULL THEN 1 ELSE 0 END) as need_topics,
                      (CASE WHEN pr.paper_id IS NULL THEN 1 ELSE 0 END) as need_refs
               FROM paper p
               LEFT JOIN (SELECT DISTINCT paper_id FROM paper_topic) pt ON p.id = pt.paper_id
               LEFT JOIN (SELECT DISTINCT paper_id FROM paper_reference) pr ON p.id = pr.paper_id
               WHERE (p.abstract = '' OR p.abstract IS NULL)
                  OR pt.paper_id IS NULL
                  OR pr.paper_id IS NULL
               ORDER BY (CASE WHEN p.abstract = '' OR p.abstract IS NULL THEN 0 ELSE 1 END),
                        (CASE WHEN pt.paper_id IS NULL THEN 0 ELSE 1 END),
                        (CASE WHEN pr.paper_id IS NULL THEN 0 ELSE 1 END)
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def paper_stats(self) -> dict:
        """Return paper statistics grouped by venue + year, with enrichment counts."""
        rows = self.conn.execute(
            """SELECT v.key as venue_key, v.name as venue_name,
                      v.ccf_rank,
                      p.year, COUNT(*) as cnt,
                      SUM(CASE WHEN p.abstract != '' AND p.abstract IS NOT NULL
                          THEN 1 ELSE 0 END) as with_abstract
               FROM paper p JOIN venue v ON p.venue_id = v.id
               GROUP BY v.key, p.year
               ORDER BY v.ccf_rank, v.key, p.year"""
        ).fetchall()
        total = self.count_papers()
        with_abstract = self.count_papers_with_abstract()
        with_topics = total - self.count_papers_without_topics()
        with_refs = total - self.count_papers_without_references()
        return {
            "total": total,
            "with_abstract": with_abstract,
            "with_topics": with_topics,
            "with_refs": with_refs,
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

        # Copy paper records (preserving IDs, flag defaults to 'unclaimed')
        for row in paper_rows:
            survey_db.conn.execute(
                """INSERT INTO paper (id, dblp_key, title, year, venue_id,
                   authors, doi, abstract, abstract_source, citation_count,
                   fetched_at, abstract_fetched_at, flag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unclaimed')""",
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

        # ── Dynamically create survey_result from output.columns ──
        extra_cols = _survey_result_columns(topic)
        col_defs = ", ".join(f"{c} TEXT DEFAULT ''" for c in extra_cols)
        survey_db.conn.execute(
            f"""CREATE TABLE survey_result (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_id INTEGER REFERENCES survey(id) ON DELETE CASCADE,
                paper_id INTEGER REFERENCES paper(id),
                include INTEGER DEFAULT NULL,
                classified_at TIMESTAMP,
                {col_defs},
                UNIQUE(survey_id, paper_id)
            )"""
        )
        survey_db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sr_survey ON survey_result(survey_id)"
        )
        survey_db.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_sr_unclassified
               ON survey_result(survey_id) WHERE include IS NULL"""
        )

        # Populate survey_result (one row per copied paper)
        survey_db.conn.executemany(
            "INSERT INTO survey_result (survey_id, paper_id) VALUES (?, ?)",
            [(survey_id, row["id"]) for row in paper_rows],
        )

        # Store column list on the instance for later use
        survey_db._survey_columns = extra_cols

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
        # Reset dynamic columns to '' alongside include=NULL
        sets = ["include = NULL", "classified_at = NULL"]
        for c in self._survey_columns:
            sets.append(f"{c} = ''")
        sql = f"UPDATE survey_result SET {', '.join(sets)} WHERE survey_id = ?"
        self.conn.execute(sql, (survey_id,))
        # Also reset paper processing flags
        self.conn.execute("UPDATE paper SET flag = 'unclaimed'")
        self.conn.commit()

    # ── Paper flag / claim mechanism ───────────────────────────

    def reset_claimed_flags(self):
        """Reset any 'claimed' flags to 'unclaimed' (crash recovery on startup)."""
        self.conn.execute(
            "UPDATE paper SET flag = 'unclaimed' WHERE flag = 'claimed'"
        )
        self.conn.commit()

    def claim_papers(
        self, survey_id: int, limit: int
    ) -> list[dict]:
        """Atomically claim up to `limit` unclaimed, unclassified papers.

        SELECT + UPDATE in a single transaction — no two callers can claim
        the same paper.  Returns the claimed rows (full paper + venue data
        plus the survey_result id).
        """
        with self.conn:  # transaction
            rows = self.conn.execute(
                """SELECT sr.id as result_id, sr.paper_id,
                          p.title, p.year, p.authors, p.doi, p.abstract,
                          p.citation_count, p.dblp_key,
                          v.name as venue_name, v.key as venue_key, v.ccf_rank
                   FROM survey_result sr
                   JOIN paper p ON sr.paper_id = p.id
                   JOIN venue v ON p.venue_id = v.id
                   WHERE sr.survey_id = ?
                     AND sr.include IS NULL
                     AND p.flag = 'unclaimed'
                   ORDER BY v.ccf_rank, p.year DESC, p.title
                   LIMIT ?""",
                (survey_id, limit),
            ).fetchall()

            if rows:
                paper_ids = [r["paper_id"] for r in rows]
                placeholders = ",".join("?" * len(paper_ids))
                self.conn.execute(
                    f"UPDATE paper SET flag = 'claimed' WHERE id IN ({placeholders})",
                    paper_ids,
                )

        return [dict(r) for r in rows]

    def get_survey_paper(
        self, survey_id: int, paper_id: int
    ) -> Optional[dict]:
        """Get a single paper with venue info from a survey DB (for debug)."""
        row = self.conn.execute(
            """SELECT sr.id as result_id, sr.paper_id,
                      p.title, p.year, p.authors, p.doi, p.abstract,
                      p.citation_count, p.dblp_key,
                      v.name as venue_name, v.key as venue_key, v.ccf_rank
               FROM survey_result sr
               JOIN paper p ON sr.paper_id = p.id
               JOIN venue v ON p.venue_id = v.id
               WHERE sr.survey_id = ? AND sr.paper_id = ?""",
            (survey_id, paper_id),
        ).fetchone()
        return dict(row) if row else None

    def search_survey_papers(
        self, survey_id: int, query: str
    ) -> list[dict]:
        """Search survey papers by title substring or exact paper_id.

        If ``query`` is a pure integer, matches paper_id exactly.
        Otherwise does a LIKE '%query%' on paper.title.
        Returns list of paper dicts (may be empty).
        """
        if query.isdigit():
            row = self.conn.execute(
                """SELECT sr.id as result_id, sr.paper_id,
                          p.title, p.year, p.authors, p.doi, p.abstract,
                          p.citation_count, p.dblp_key,
                          v.name as venue_name, v.key as venue_key, v.ccf_rank
                   FROM survey_result sr
                   JOIN paper p ON sr.paper_id = p.id
                   JOIN venue v ON p.venue_id = v.id
                   WHERE sr.survey_id = ? AND sr.paper_id = ?""",
                (survey_id, int(query)),
            ).fetchone()
            return [dict(row)] if row else []

        rows = self.conn.execute(
            """SELECT sr.id as result_id, sr.paper_id,
                      p.title, p.year, p.authors, p.doi, p.abstract,
                      p.citation_count, p.dblp_key,
                      v.name as venue_name, v.key as venue_key, v.ccf_rank
               FROM survey_result sr
               JOIN paper p ON sr.paper_id = p.id
               JOIN venue v ON p.venue_id = v.id
               WHERE sr.survey_id = ? AND p.title LIKE ?
               ORDER BY v.ccf_rank, p.year DESC""",
            (survey_id, f"%{query}%"),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_paper_classified(self, paper_id: int):
        """Mark a paper's flag as 'classified' after survey_result is written."""
        self.conn.execute(
            "UPDATE paper SET flag = 'classified' WHERE id = ?",
            (paper_id,),
        )
        self.conn.commit()

    # ── Progress / ETA ────────────────────────────────────────

    def _save_progress_snapshot(
        self, survey_id: int, classified: int, total: int
    ) -> Optional[dict]:
        """Save current progress and return the *previous* snapshot (or None).

        Only the most recent snapshot is kept per survey — old rows are deleted
        before inserting the new one.
        """
        # Read the previous snapshot before deleting it
        prev = self.conn.execute(
            """SELECT classified_count, total_count, recorded_at
               FROM survey_progress_log
               WHERE survey_id = ?
               ORDER BY recorded_at DESC
               LIMIT 1""",
            (survey_id,),
        ).fetchone()

        # Delete all old snapshots for this survey — keep only the new one
        self.conn.execute(
            "DELETE FROM survey_progress_log WHERE survey_id = ?",
            (survey_id,),
        )

        self.conn.execute(
            """INSERT INTO survey_progress_log (survey_id, classified_count, total_count, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (survey_id, classified, total, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

        return dict(prev) if prev else None

    def _compute_eta(self, survey_id: int, classified: int, total: int) -> Optional[str]:
        """Compute ETA based on progress rate since the previous snapshot.

        Saves the current snapshot (keeping only the latest), then compares
        with the previous one to estimate remaining time.

        Returns a human-readable string like "约 2 小时 15 分钟", or None if
        there isn't enough data yet.
        """
        prev = self._save_progress_snapshot(survey_id, classified, total)

        if prev is None:
            return None

        prev_classified = prev["classified_count"]

        if prev_classified >= classified:
            return None  # no progress since last check

        prev_time = datetime.fromisoformat(prev["recorded_at"])
        now = datetime.now(timezone.utc)
        elapsed = (now - prev_time).total_seconds()

        if elapsed <= 0:
            return None

        rate = (classified - prev_classified) / elapsed  # papers/sec
        remaining = total - classified

        if remaining <= 0 or rate <= 0:
            return None

        eta_seconds = remaining / rate

        # Format: HH:MM:SS
        total_seconds = int(eta_seconds)
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        return f"{h:02}:{m:02}:{s:02}"

    def survey_stats(self, survey_id: int) -> dict:
        """Return classification progress stats.

        include: 1 = include in survey, 0 = exclude, NULL = unclassified.
        """
        total_row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM survey_result WHERE survey_id = ?",
            (survey_id,),
        ).fetchone()
        classified_row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM survey_result
               WHERE survey_id = ? AND include IS NOT NULL""",
            (survey_id,),
        ).fetchone()
        relevant_row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM survey_result
               WHERE survey_id = ? AND include = 1""",
            (survey_id,),
        ).fetchone()

        total = total_row["cnt"] if total_row else 0
        classified = classified_row["cnt"] if classified_row else 0
        relevant = relevant_row["cnt"] if relevant_row else 0

        if classified == 0:
            status = "pending"
        elif classified < total:
            status = "running"
        else:
            status = "completed"

        eta = None
        if status == "running":
            eta = self._compute_eta(survey_id, classified, total)

        return {
            "survey_id": survey_id,
            "total": total,
            "classified": classified,
            "unclassified": total - classified,
            "relevant": relevant,
            "not_relevant": classified - relevant,
            "progress_pct": round(classified / total * 100, 1) if total > 0 else 0,
            "status": status,
            "eta": eta,
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
               WHERE sr.survey_id = ? AND sr.include IS NULL
               ORDER BY v.ccf_rank, p.year DESC, p.title
               LIMIT ?""",
            (survey_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_result(
        self, result_id: int, include: int = 0,
        columns: Optional[dict[str, str]] = None,
    ):
        """Mark a single survey_result as classified.

        include: 1 = include in survey, 0 = exclude.
        columns: {field_name: value, ...} for the dynamic columns.
        """
        sets = ["include = ?", "classified_at = ?"]
        params: list = [include, datetime.now(timezone.utc).isoformat()]
        if columns:
            for k, v in columns.items():
                if k in self._survey_columns:  # safety: only write real columns
                    sets.append(f"{k} = ?")
                    params.append(v)
        params.append(result_id)
        sql = f"UPDATE survey_result SET {', '.join(sets)} WHERE id = ?"
        self.conn.execute(sql, params)
        self.conn.commit()

    def mark_batch(self, results: list[dict]):
        """Batch mark survey_results. Each dict: {id, include, columns: {k:v}}."""
        now = datetime.now(timezone.utc).isoformat()
        # Build per-row params from the columns dict in each result
        all_cols = set()
        for r in results:
            cols = r.get("columns", {}) or {}
            all_cols.update(cols.keys())
        col_list = sorted(all_cols)
        sets = ["include = ?", "classified_at = ?"] + [f"{c} = ?" for c in col_list]
        sql = f"UPDATE survey_result SET {', '.join(sets)} WHERE id = ?"
        rows = []
        for r in results:
            cols = r.get("columns", {}) or {}
            row = [r.get("include", 0), now] + [cols.get(c, "") for c in col_list] + [r["id"]]
            rows.append(row)
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def get_survey_results(
        self, survey_id: int,
    ) -> list[dict]:
        """Get relevant classified results for a survey.

        Always filters include=1 — export is for included papers only.
        """
        where = "sr.survey_id = ? AND sr.include = 1"

        # Dynamic SELECT: include + all survey_result columns
        sr_cols = ", ".join([f"sr.{c}" for c in self._survey_columns])
        if sr_cols:
            sr_cols = ", " + sr_cols

        sql = f"""SELECT v.name AS venue_name, v.ccf_rank AS venue_ccf_rank,
                         v.type AS venue_type, v.key AS venue_key,
                         p.title AS paper_title, p.year AS paper_year,
                         p.doi AS paper_doi, p.authors AS paper_authors,
                         p.abstract AS paper_abstract,
                         p.citation_count AS paper_citation_count,
                         p.dblp_key AS paper_dblp_key,
                         sr.include{sr_cols}
                 FROM survey_result sr
                 JOIN paper p ON sr.paper_id = p.id
                 JOIN venue v ON p.venue_id = v.id
                 WHERE {where}
                 ORDER BY v.ccf_rank, p.year DESC, p.title"""

        rows = self.conn.execute(sql, (survey_id,)).fetchall()
        return [dict(r) for r in rows]
