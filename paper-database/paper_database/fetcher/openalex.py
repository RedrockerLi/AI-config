"""OpenAlex API client — abstract fetcher with batch DOI lookup.

OpenAlex: https://api.openalex.org/
Free API key gives 100,000 credits/day + 10 req/s (polite pool).
Without key: 100 credits/day (~10 list queries).
Get a key: https://openalex.org/settings/api

Set OPENALEX_API_KEY environment variable to use a key.

Credit usage:
  - Single work by DOI:  1 credit   (GET /works/https://doi.org/...)
  - List / filter:       10 credits (GET /works?filter=doi:a|b|...)
  - Batch DOI filter:    10 credits for up to 50 DOIs (pipe-delimited)

Adaptive rate limiting:
  - _current_delay starts at 0.5s, doubles on 429 (max 5s), decays on success
  - 3 consecutive 429s → stop retrying (persistent quota exhaustion)
"""

import os
import time
from typing import Optional

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class OpenAlexFetcher(AbstractFetcher):
    """Fetches abstracts from OpenAlex API as fallback when Semantic Scholar fails.

    自适应速率控制:
    - _current_delay: 遇 429 翻倍（上限 _MAX_DELAY），成功后向 _MIN_DELAY 衰减
    - 连续 3 次 429 → 持久限流，停止重试
    """

    SEARCH_URL = "https://api.openalex.org/works"

    # Adaptive rate-limit constants
    _MIN_DELAY = 0.1        # 最快 10 req/s（polite pool 上限）
    _MAX_DELAY = 5.0        # 连续 429 后最大延迟
    _BATCH_CHUNK_SIZE = 50  # 每批最多 DOIs（API pipe 分隔上限）

    def __init__(self, timeout: float = 30.0, delay: float = 0.5):
        """
        Args:
            timeout: HTTP request timeout in seconds.
            delay: Base seconds between requests. Seeds adaptive delay.
                   With API key: 0.5s → 0.1s on success (10 req/s max).
                   Without key: suggest 3s+ for the shared free pool.
        """
        self.timeout = timeout
        self._base_delay = delay
        self._current_delay = delay  # 自适应，随 429/成功动态调整
        self._consecutive_429s = 0
        self._api_key = os.environ.get("OPENALEX_API_KEY", "")
        self._headers = {}
        if self._api_key:
            self._headers["User-Agent"] = (
                "paper-database/0.2 (mailto:paper-database@example.com)"
            )

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """OpenAlex is not ideal for venue listing. Use DBLP for that."""
        return []

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """Search OpenAlex by DOI (preferred) or title and retrieve abstract.

        节流由 _fetch_with_retry 自适应处理，不再手动 sleep。
        """
        # Prefer DOI search if available
        if paper.doi:
            result = self._search_by_doi(paper.doi)
            if result:
                return result

        # Fallback to title search
        return self._search_by_title(paper.title)

    def fetch_abstracts_batch(
        self, papers: list[PaperMeta]
    ) -> dict[str, str]:
        """Fetch abstracts for multiple papers — DOI batch + title fallback.

        Papers with DOIs are looked up via pipe-delimited batch filter
        (up to 50 DOIs per request, 10 credits per batch). Papers without
        DOIs, plus batch misses, fall back to individual title search.

        Returns:
            dict mapping dblp_key -> abstract.
        """
        results: dict[str, str] = {}

        # ── Phase 1: Split by DOI availability ──────────────────
        papers_with_doi: list[PaperMeta] = []
        papers_without_doi: list[PaperMeta] = []

        for p in papers:
            if p.doi.strip():
                papers_with_doi.append(p)
            else:
                papers_without_doi.append(p)

        # ── Phase 2: Batch DOI lookup ───────────────────────────
        if papers_with_doi:
            doi_to_paper: dict[str, PaperMeta] = {}
            for p in papers_with_doi:
                doi = p.doi.strip()
                # Normalize: strip "https://doi.org/" prefix if present
                if doi.startswith("https://doi.org/"):
                    doi = doi[len("https://doi.org/"):]
                doi_to_paper[doi] = p

            batch_results = self._fetch_batch_by_dois(
                list(doi_to_paper.keys())
            )

            matched_dois: set[str] = set()
            for doi, info in batch_results.items():
                paper = doi_to_paper.get(doi)
                if paper is None:
                    continue
                abstract = (info.get("abstract") or "").strip()
                if abstract:
                    results[paper.dblp_key] = abstract
                    matched_dois.add(doi)

            # Unmatched DOIs -> title fallback
            for doi, paper in doi_to_paper.items():
                if doi not in matched_dois:
                    papers_without_doi.append(paper)

        # ── Phase 3: Individual title search ────────────────────
        for paper in papers_without_doi:
            abstract = self._search_by_title(paper.title)
            if abstract:
                results[paper.dblp_key] = abstract

        return results

    def _fetch_batch_by_dois(
        self, dois: list[str]
    ) -> dict[str, dict]:
        """Batch-lookup works by up to 50 DOIs via pipe-delimited filter.

        ``GET /works?filter=doi:a|b|...&per_page=200&select=...``
        10 credits per batch regardless of DOI count (up to 50).

        Args:
            dois: Plain DOI strings, e.g. "10.1234/abcde".

        Returns:
            {normalized_doi: {"abstract": str, "doi": str, "title": str}}
        """
        results: dict[str, dict] = {}

        for i in range(0, len(dois), self._BATCH_CHUNK_SIZE):
            chunk = dois[i:i + self._BATCH_CHUNK_SIZE]
            doi_filter = "|".join(chunk)
            batch_label = f"batch-{i // self._BATCH_CHUNK_SIZE + 1}"

            params = self._build_params(
                filter=f"doi:{doi_filter}",
                per_page=200,
            )
            # Override select: we need doi for reverse-matching
            params["select"] = "id,doi,title,abstract_inverted_index"

            data = self._fetch_with_retry(
                self.SEARCH_URL, params, label=batch_label
            )
            if data is None:
                continue

            works = data.get("results", [])
            for work in works:
                work_doi = (work.get("doi") or "").strip()
                if not work_doi:
                    continue
                # Normalize: OpenAlex returns full URL, strip prefix
                if work_doi.startswith("https://doi.org/"):
                    work_doi = work_doi[len("https://doi.org/"):]

                results[work_doi] = {
                    "abstract": self._extract_abstract(work) or "",
                    "doi": work_doi,
                    "title": work.get("title", "") or "",
                }

        return results

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
        """GET + 指数退避重试 + 429 自适应降速。

        首次请求前自动 sleep _current_delay（自适应节流）。
        429: 计数器+1，延迟翻倍，连续 3 次 → 止损返回 None。
        404: 不重试，直接返回 None。
        其余瞬时错误: 标准指数退避 (2s, 4s, 8s)。
        """
        last_error = ""

        for attempt in range(max_retries):
            # 自适应节流 — 每次新请求前 sleep（含重试后的新请求）
            if attempt == 0:
                time.sleep(self._current_delay)

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

                # ── 429 专项处理 ──────────────────────────────────
                if response.status_code == 429:
                    self._consecutive_429s += 1
                    if self._consecutive_429s >= 3:
                        tag = f" [{label}]" if label else ""
                        print(
                            f"  [OpenAlex]{tag} 连续 {self._consecutive_429s} 次 429, "
                            f"配额可能已耗尽，停止重试。"
                            f"请检查 OPENALEX_API_KEY 是否正确设置。"
                        )
                        return None

                    # 自适应降速: 延迟翻倍
                    self._current_delay = min(
                        self._MAX_DELAY, self._current_delay * 2.0
                    )
                    tag = f" [{label}]" if label else ""
                    print(
                        f"  [OpenAlex]{tag} 429 限流 "
                        f"(#{self._consecutive_429s}), "
                        f"后续延迟升至 {self._current_delay:.1f}s"
                    )
                    last_error = "429"
                    continue

                # ── 成功 ──────────────────────────────────────────
                # 延迟向 min 衰减，429 计数器清零
                self._current_delay = max(
                    self._MIN_DELAY, self._current_delay * 0.9
                )
                self._consecutive_429s = 0

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                # 429 already handled above, but guard against edge cases
                if e.response.status_code == 429:
                    continue
                last_error = str(e)
            except (httpx.TimeoutException, httpx.RequestError) as e:
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
