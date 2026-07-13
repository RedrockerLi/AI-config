"""OpenAlex API client — fallback abstract fetcher.

OpenAlex: https://api.openalex.org/
No API key required. Rate limit: ~10 req/s.
"""

from typing import Optional

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class OpenAlexFetcher(AbstractFetcher):
    """Fetches abstracts from OpenAlex API as fallback when Semantic Scholar fails."""

    SEARCH_URL = "https://api.openalex.org/works"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """OpenAlex is not ideal for venue listing. Use DBLP for that."""
        return []

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """Search OpenAlex by title (or DOI) and retrieve abstract."""

        # Prefer DOI search if available
        if paper.doi:
            result = self._search_by_doi(paper.doi)
            if result:
                return result

        # Fallback to title search
        return self._search_by_title(paper.title)

    def _search_by_doi(self, doi: str) -> Optional[str]:
        """Search OpenAlex by DOI."""
        params = {
            "filter": f"doi:{doi}",
            "select": "title,abstract_inverted_index,authorships,cited_by_count",
            "per_page": 1,
        }
        try:
            response = httpx.get(
                self.SEARCH_URL, params=params, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None

        results = data.get("results", [])
        if not results:
            return None

        return self._extract_abstract(results[0])

    def _search_by_title(self, title: str) -> Optional[str]:
        """Search OpenAlex by title."""
        # Clean title
        query = title.strip().rstrip(".")
        if len(query) > 300:
            query = query[:300]

        params = {
            "search": query,
            "select": "title,abstract_inverted_index,authorships,cited_by_count",
            "per_page": 3,
        }
        try:
            response = httpx.get(
                self.SEARCH_URL, params=params, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None

        results = data.get("results", [])
        if not results:
            return None

        # Find best title match
        title_lower = title.lower().rstrip(".")
        best = None
        best_score = 0

        for r in results:
            r_title = (r.get("title") or "").lower().rstrip(".")
            if not r_title:
                continue

            t_words = set(title_lower.split())
            r_words = set(r_title.split())
            if not t_words or not r_words:
                continue

            intersection = t_words & r_words
            union = t_words | r_words
            score = len(intersection) / len(union) if union else 0

            if score > best_score:
                best_score = score
                best = r

        if best is None or best_score < 0.3:
            return None

        return self._extract_abstract(best)

    @staticmethod
    def _extract_abstract(work: dict) -> Optional[str]:
        """OpenAlex stores abstracts as an inverted index. Reconstruct the text."""
        inverted = work.get("abstract_inverted_index")
        if not inverted or not isinstance(inverted, dict):
            return None

        # Reconstruct: {word: [positions]} → sorted word list
        word_positions: list[tuple[str, int]] = []
        for word, positions in inverted.items():
            if not isinstance(positions, list):
                continue
            for pos in positions:
                if isinstance(pos, int):
                    word_positions.append((word, pos))

        if not word_positions:
            return None

        word_positions.sort(key=lambda x: x[1])
        return " ".join(w for w, _ in word_positions)
