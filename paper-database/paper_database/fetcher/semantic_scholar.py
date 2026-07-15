"""Semantic Scholar API client — fetch abstracts by title or batch DOI lookup.

Free tier (no API key): ~100 req / 5 min (shared global quota)
With API key: 1 req/s across all endpoints
Set S2_API_KEY environment variable for higher rate limits.
"""

import os
import time
from typing import Optional

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class SemanticScholarFetcher(AbstractFetcher):
    """Fetches abstracts from Semantic Scholar API.

    Uses DOI batch lookup (/paper/batch) for papers with DOIs,
    title-based search (/paper/search) as fallback.
    """

    SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"

    # Batch endpoint max IDs per request
    BATCH_CHUNK_SIZE = 500

    def __init__(self, timeout: float = 30.0, delay: float = 3.0):
        """
        Args:
            timeout: HTTP request timeout in seconds.
            delay: Seconds between individual title-search requests.
                   Without API key: 3s (100 req / 5 min).
                   With API key: 0.02s (~50 req/s, safe below 100 req/s).
        """
        self.timeout = timeout
        self.delay = delay
        self._api_key = os.environ.get("S2_API_KEY", "")
        self._headers = {}
        if self._api_key:
            self._headers["x-api-key"] = self._api_key

    # ── AbstractFetcher interface ──────────────────────────────

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """Semantic Scholar is not ideal for venue listing. Use DBLP for that."""
        return []

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """Search Semantic Scholar by title and get abstract + references."""
        query = paper.title.strip().rstrip(".")
        if len(query) > 200:
            query = query[:200]

        params = {
            "query": query,
            "limit": 3,
            "fields": "title,abstract,year,authors,citationCount,externalIds,"
                      "references.title,references.citationCount",
        }

        try:
            response = httpx.get(
                self.SEARCH_URL,
                params=params,
                headers=self._headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"  [S2] Search error for '{paper.title[:60]}...': {e}")
            return None

        papers = data.get("data", [])
        if not papers:
            return None

        best = self._find_best_match(paper.title, papers)
        if best is None:
            return None

        abstract = best.get("abstract") or ""
        if best.get("citationCount"):
            paper.citation_count = best["citationCount"]
        ext_ids = best.get("externalIds", {}) or {}
        if ext_ids.get("DOI") and not paper.doi:
            paper.doi = ext_ids["DOI"]

        # Populate references from title search result
        refs = best.get("references") or []
        paper.references = [
            r.get("title", "") for r in refs
            if r and r.get("title")
        ]

        return abstract.strip() if abstract else None

    def fetch_abstracts_batch(
        self, papers: list[PaperMeta], db=None
    ) -> dict[str, str]:
        """Batch fetch abstracts for multiple papers — 逐批写入，防 Ctrl+C 丢失.

        Papers with DOIs are fetched via the /paper/batch endpoint
        (up to 500 per request). Papers without DOIs, plus any
        batch misses, fall back to individual title search.

        If ``db`` is provided, abstracts are written to the database
        **immediately** as each batch completes.

        Returns:
            dict mapping dblp_key -> abstract.
        """
        results: dict[str, str] = {}

        def _save(paper, abstract, references=None):
            """Write to DB immediately if available, else accumulate."""
            if db is not None:
                db.update_paper_abstract(
                    paper.dblp_key, abstract, "semantic_scholar",
                    citation_count=paper.citation_count,
                    doi=paper.doi,
                )
                if references:
                    paper_id = db.get_paper_id_by_dblp_key(paper.dblp_key)
                    if paper_id:
                        db.save_paper_references(
                            paper_id, "semantic_scholar",
                            [{"title": r["title"],
                              "citation_count": r.get("citation_count", 0)}
                             for r in references],
                        )
            results[paper.dblp_key] = abstract

        # ── Phase 1: Split by DOI availability ──────────────────
        papers_with_doi: list[PaperMeta] = []
        papers_without_doi: list[PaperMeta] = []

        for p in papers:
            if p.doi.strip():
                papers_with_doi.append(p)
            else:
                papers_without_doi.append(p)

        # ── Phase 2: Batch DOI lookup (逐批保存) ─────────────────
        if papers_with_doi:
            doi_to_papers: dict[str, PaperMeta] = {}
            for p in papers_with_doi:
                doi = p.doi.strip()
                if doi.startswith("https://doi.org/"):
                    doi = doi[len("https://doi.org/"):]
                doi = doi.lower()
                doi_to_papers[doi] = p

            dois = list(doi_to_papers.keys())
            total_batches = (len(dois) + self.BATCH_CHUNK_SIZE - 1) // self.BATCH_CHUNK_SIZE
            matched_dois: set[str] = set()

            for i in range(0, len(dois), self.BATCH_CHUNK_SIZE):
                chunk = dois[i:i + self.BATCH_CHUNK_SIZE]
                batch_num = i // self.BATCH_CHUNK_SIZE + 1
                label = f"S2 batch-{batch_num}/{total_batches}" if total_batches > 1 else "S2 batch"

                if total_batches > 1:
                    print(f"  [S2] {label} ({len(chunk)} DOIs)...", end=" ", flush=True)

                batch_results = self._fetch_one_doi_batch(chunk)
                batch_saved = 0

                for doi, info in batch_results.items():
                    paper = doi_to_papers.get(doi)
                    if paper is None:
                        continue

                    abstract = (info.get("abstract") or "").strip()
                    refs = info.get("references") or []
                    if abstract:
                        _save(paper, abstract, references=refs)
                        batch_saved += 1
                        matched_dois.add(doi)
                    elif refs:
                        # Save references even if no abstract
                        if db is not None:
                            paper_id = db.get_paper_id_by_dblp_key(paper.dblp_key)
                            if paper_id:
                                db.save_paper_references(
                                    paper_id, "semantic_scholar",
                                    [{"title": r["title"],
                                      "citation_count": r.get("citation_count", 0)}
                                     for r in refs],
                                )
                    if info.get("citationCount"):
                        paper.citation_count = info["citationCount"]
                    s2_doi = info.get("doi") or ""
                    if s2_doi and not paper.doi:
                        paper.doi = s2_doi

                if total_batches > 1:
                    print(f"{batch_saved} 篇")

            if total_batches > 1:
                print(f"  [S2] 批量完成: {len(matched_dois)}/{len(doi_to_papers)} DOI 命中")

            # Unmatched DOIs → retry via title search
            for doi, paper in doi_to_papers.items():
                if doi not in matched_dois:
                    papers_without_doi.append(paper)

        # ── Individual title search ────────────────────
        if papers_without_doi:
            effective_delay = 0.02 if self._api_key else self.delay
            for paper in papers_without_doi:
                abstract = self.fetch_abstract(paper)
                if abstract:
                    refs = [{"title": t} for t in paper.references] if paper.references else None
                    _save(paper, abstract, references=refs)
                time.sleep(effective_delay)

        return results

    # ── Internal helpers ───────────────────────────────────────

    def _fetch_one_doi_batch(self, dois: list[str]) -> dict[str, dict]:
        """POST /graph/v1/paper/batch — lookup up to 500 papers by ID
        (single batch, no internal loop).

        Returns:
            {lowercase_doi: {"abstract": str, "doi": str, "title": str,
                             "references": list[dict]}}
        """
        results: dict[str, dict] = {}
        ids = [f"DOI:{doi}" for doi in dois]

        try:
            response = httpx.post(
                self.BATCH_URL,
                params={"fields": "title,abstract,citationCount,externalIds,"
                                  "references.title,references.citationCount"},
                json={"ids": ids},
                headers=self._headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"  [S2] Batch error ({len(dois)} DOIs): {e}")
            return results

        for paper in data:
            if paper is None:
                continue
            ext = paper.get("externalIds") or {}
            doi = (ext.get("DOI") or "").strip().lower()
            if doi:
                refs = paper.get("references") or []
                results[doi] = {
                    "abstract": paper.get("abstract") or "",
                    "citationCount": paper.get("citationCount") or 0,
                    "doi": doi,
                    "title": paper.get("title") or "",
                    "references": [
                        {"title": r.get("title", ""),
                         "citation_count": r.get("citationCount", 0)}
                        for r in refs if r and r.get("title")
                    ],
                }

        return results

    @staticmethod
    def _find_best_match(title: str, candidates: list[dict]) -> Optional[dict]:
        """Simple title matching: find candidate with best word overlap."""
        if not candidates:
            return None

        title_lower = title.lower().strip().rstrip(".")
        title_words = set(title_lower.split())

        best_score = 0
        best_candidate = candidates[0]

        for c in candidates:
            c_title = (c.get("title") or "").lower().strip().rstrip(".")
            c_words = set(c_title.split())

            if not title_words or not c_words:
                continue

            intersection = title_words & c_words
            union = title_words | c_words
            score = len(intersection) / len(union) if union else 0

            if score > best_score:
                best_score = score
                best_candidate = c

        if best_score < 0.3:
            return None

        return best_candidate
