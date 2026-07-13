"""Semantic Scholar API client — fetch abstracts by title matching.

Free tier: 1 req/s without API key, 100 req/s with key.
Set S2_API_KEY environment variable for higher rate limits.
"""

import os
import time
from typing import Optional

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class SemanticScholarFetcher(AbstractFetcher):
    """Fetches abstracts from Semantic Scholar API.

    Uses title-based search to match papers, then retrieves abstracts.
    """

    SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"

    def __init__(self, timeout: float = 30.0, delay: float = 1.0):
        self.timeout = timeout
        self.delay = delay
        self._api_key = os.environ.get("S2_API_KEY", "")
        self._headers = {}
        if self._api_key:
            self._headers["x-api-key"] = self._api_key

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """Semantic Scholar is not ideal for venue listing. Use DBLP for that."""
        return []

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """Search Semantic Scholar by title and get abstract."""
        # Clean title for search query
        query = paper.title.strip().rstrip(".")
        # Truncate if too long
        if len(query) > 200:
            query = query[:200]

        params = {
            "query": query,
            "limit": 3,
            "fields": "title,abstract,year,authors,citationCount,externalIds",
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

        # Find best match by title similarity
        best = self._find_best_match(paper.title, papers)
        if best is None:
            return None

        abstract = best.get("abstract") or ""
        # Update citation count if available
        if best.get("citationCount"):
            paper.citation_count = best["citationCount"]
        # Update DOI if we don't have one
        ext_ids = best.get("externalIds", {}) or {}
        if ext_ids.get("DOI") and not paper.doi:
            paper.doi = ext_ids["DOI"]

        return abstract.strip() if abstract else None

    def fetch_abstracts_batch(
        self, papers: list[PaperMeta]
    ) -> dict[str, str]:
        """Batch fetch abstracts. Uses individual search since S2 batch
        requires paper IDs, not titles."""
        results = {}
        for paper in papers:
            if self._api_key:
                time.sleep(0.02)  # ~50 req/s with key (conservative)
            else:
                time.sleep(self.delay)

            abstract = self.fetch_abstract(paper)
            if abstract:
                results[paper.dblp_key] = abstract
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

            # Jaccard similarity on word sets
            intersection = title_words & c_words
            union = title_words | c_words
            score = len(intersection) / len(union) if union else 0

            if score > best_score:
                best_score = score
                best_candidate = c

        # Require minimum similarity
        if best_score < 0.3:
            return None

        return best_candidate
