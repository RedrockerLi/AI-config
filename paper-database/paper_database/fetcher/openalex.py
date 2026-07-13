"""OpenAlex API client — fallback abstract fetcher.

OpenAlex: https://api.openalex.org/
Free API key required for meaningful usage (100,000 credits/day).
Without key: 100 credits/day (~10 list queries).
Get a key: https://openalex.org/settings/api

Set OPENALEX_API_KEY environment variable to use a key.
"""

import os
import time
from typing import Optional

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class OpenAlexFetcher(AbstractFetcher):
    """Fetches abstracts from OpenAlex API as fallback when Semantic Scholar fails."""

    SEARCH_URL = "https://api.openalex.org/works"

    def __init__(self, timeout: float = 30.0, delay: float = 0.5):
        """
        Args:
            timeout: HTTP request timeout in seconds.
            delay: Seconds between requests. With API key: 0.5s (~2 req/s
                   for list queries at 10 credits each, safe under the
                   100,000 credit daily cap).
        """
        self.timeout = timeout
        self.delay = delay
        self._api_key = os.environ.get("OPENALEX_API_KEY", "")
        self._headers = {}
        if self._api_key:
            self._headers["User-Agent"] = (
                "paper-database/0.1 (mailto:paper-database@example.com)"
            )

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """OpenAlex is not ideal for venue listing. Use DBLP for that."""
        return []

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """Search OpenAlex by DOI (preferred) or title and retrieve abstract."""
        try:
            # Prefer DOI search if available
            if paper.doi:
                result = self._search_by_doi(paper.doi)
                if result:
                    return result

            # Fallback to title search
            result = self._search_by_title(paper.title)
            return result
        finally:
            # Always delay between requests
            time.sleep(self.delay)

    def _search_by_doi(self, doi: str) -> Optional[str]:
        """Search OpenAlex by DOI."""
        params = self._build_params(
            filter=f"doi:{doi}",
            per_page=1,
        )
        data = self._fetch_with_retry(self.SEARCH_URL, params, label=doi)
        if data is None:
            return None

        results = data.get("results", [])
        if not results:
            return None

        return self._extract_abstract(results[0])

    def _search_by_title(self, title: str) -> Optional[str]:
        """Search OpenAlex by title."""
        query = title.strip().rstrip(".")
        if len(query) > 300:
            query = query[:300]

        params = self._build_params(
            search=query,
            per_page=3,
        )
        data = self._fetch_with_retry(self.SEARCH_URL, params)
        if data is None:
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

    def _fetch_with_retry(
        self, url: str, params: dict, label: str = "", max_retries: int = 3
    ) -> Optional[dict]:
        """GET + 指数退避重试。404 不重试，其余异常均重试。"""
        last_error = ""
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 2 ** attempt  # 2s, 4s, 8s
                tag = f" [{label}]" if label else ""
                print(
                    f"  [OpenAlex]{tag} 重试 ({attempt + 1}/{max_retries}), "
                    f"等待 {wait}s..."
                )
                time.sleep(wait)

            try:
                response = httpx.get(
                    url, params=params, headers=self._headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                last_error = str(e)
            except Exception as e:
                last_error = str(e)

        tag = f" [{label}]" if label else ""
        print(f"  [OpenAlex]{tag} 持久错误: {last_error}")
        return None

    def _build_params(self, **kwargs) -> dict:
        """Build query params, adding api_key if available."""
        params = dict(kwargs)
        params.setdefault("select", "title,abstract_inverted_index,authorships,cited_by_count")
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    @staticmethod
    def _extract_abstract(work: dict) -> Optional[str]:
        """OpenAlex stores abstracts as an inverted index. Reconstruct the text."""
        inverted = work.get("abstract_inverted_index")
        if not inverted or not isinstance(inverted, dict):
            return None

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
