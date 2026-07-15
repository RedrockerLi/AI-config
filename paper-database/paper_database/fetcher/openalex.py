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


# URL ↔ short ID conversion (used by OpenAlex fetcher only)
def _extract_oa_id(url: str) -> str:
    """'https://openalex.org/W123' → 'W123'"""
    return url.rstrip("/").split("/")[-1]


def _build_oa_url(short_id: str) -> str:
    """'W123' → 'https://openalex.org/W123'"""
    return f"https://openalex.org/{short_id}"


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
        self, papers: list[PaperMeta], db=None, doi_only: bool = False,
        fetch_references: bool = False,
    ) -> dict[str, str]:
        """Fetch abstracts for multiple papers — DOI batch + title fallback.

        Papers with DOIs are looked up via pipe-delimited batch filter
        (up to 50 DOIs per request, 10 credits per batch). Papers without
        DOIs, plus batch misses, fall back to individual title search.

        If ``db`` is provided, abstracts, topics (concepts), and references
        are written to the database **immediately** as each batch completes
        (survives Ctrl+C).

        ``fetch_references`` enables a second-phase batch API call to resolve
        OpenAlex work IDs to titles. Costs extra credits — off by default.

        Returns:
            dict mapping dblp_key -> abstract.
        """
        results: dict[str, str] = {}

        def _save(paper, abstract, concepts=None):
            """Write to DB immediately if available, else accumulate."""
            if db is not None:
                db.update_paper_abstract(
                    paper.dblp_key, abstract, "openalex",
                    citation_count=paper.citation_count,
                    doi=paper.doi,
                )
                paper_id = db.get_paper_id_by_dblp_key(paper.dblp_key)
                if paper_id:
                    if concepts:
                        db.save_paper_topics(paper_id, "openalex", concepts)
            results[paper.dblp_key] = abstract

        # ── Split by DOI availability ──────────────────────────
        papers_with_doi: list[PaperMeta] = []
        papers_without_doi: list[PaperMeta] = []

        for p in papers:
            if p.doi.strip():
                papers_with_doi.append(p)
            else:
                papers_without_doi.append(p)

        # ── Batch DOI lookup (逐批保存，防 Ctrl+C 丢失) ─────────
        if papers_with_doi:
            doi_to_paper: dict[str, PaperMeta] = {}
            for p in papers_with_doi:
                doi = p.doi.strip()
                # Normalize: strip prefix + lowercase (DOIs are case-insensitive)
                if doi.startswith("https://doi.org/"):
                    doi = doi[len("https://doi.org/"):]
                doi = doi.lower()
                doi_to_paper[doi] = p

            dois = list(doi_to_paper.keys())
            total_batches = (len(dois) + self._BATCH_CHUNK_SIZE - 1) // self._BATCH_CHUNK_SIZE
            matched_dois: set[str] = set()
            batch_saved = 0
            topics_saved = 0
            ref_urls_saved = 0

            for i in range(0, len(dois), self._BATCH_CHUNK_SIZE):
                chunk = dois[i:i + self._BATCH_CHUNK_SIZE]
                batch_num = i // self._BATCH_CHUNK_SIZE + 1

                # ── Fetch one batch ─────────────────────────────
                batch_results = self._fetch_one_doi_batch(chunk, batch_num, total_batches)

                # ── Save immediately ────────────────────────────
                for doi, info in batch_results.items():
                    paper = doi_to_paper.get(doi)
                    if paper is None:
                        continue
                    abstract = (info.get("abstract") or "").strip()
                    concepts = info.get("concepts") or []
                    if abstract:
                        _save(paper, abstract, concepts=concepts)
                        batch_saved += 1
                        matched_dois.add(doi)
                        if concepts:
                            topics_saved += 1
                    elif concepts:
                        # Save topics even if no abstract
                        if db is not None:
                            paper_id = db.get_paper_id_by_dblp_key(paper.dblp_key)
                            if paper_id:
                                db.save_paper_topics(paper_id, "openalex", concepts)
                                topics_saved += 1
                    # Always save referenced_works URLs (zero extra API cost)
                    # --fetch-references controls phase-2: resolve URLs → titles
                    ref_ids = info.get("referenced_works") or []
                    if ref_ids and db is not None:
                        paper_id = db.get_paper_id_by_dblp_key(paper.dblp_key)
                        if paper_id:
                            db.save_paper_reference_ids(paper_id, "openalex", ref_ids)
                            ref_urls_saved += 1

            if total_batches > 1:
                print(
                    f"  [OpenAlex] DOI 批量完成: "
                    f"摘要 {batch_saved} | 主题标签 {topics_saved} | "
                    f"引用URL {ref_urls_saved} 篇 "
                    f"({len(matched_dois)}/{len(doi_to_paper)} DOI 命中)"
                )

            # Unmatched DOIs -> title fallback
            unmatched = sum(1 for doi in doi_to_paper if doi not in matched_dois)
            if unmatched > 0:
                print(
                    f"  [OpenAlex] {unmatched} 篇 DOI 未命中，回退到标题搜索"
                )
            for doi, paper in doi_to_paper.items():
                if doi not in matched_dois:
                    papers_without_doi.append(paper)

        # ── Individual title search ────────────────────────────
        if not doi_only and papers_without_doi:
            print(
                f"  [OpenAlex] {len(papers_without_doi)} 篇无 DOI / 未命中，"
                f"标题搜索 (10 credits/篇)..."
            )
            for paper in papers_without_doi:
                abstract = self._search_by_title(paper.title)
                if abstract:
                    _save(paper, abstract)
        elif doi_only and papers_without_doi:
            print(
                f"  [OpenAlex] {len(papers_without_doi)} 篇无 DOI / 未命中，"
                f"DOI-only 模式跳过"
            )

        if papers:
            doi_count = sum(1 for p in papers if p.doi.strip())
            mode = "DOI-only" if doi_only else "完整"
            if fetch_references:
                mode += "+refs"
            print(
                f"  [OpenAlex] 本批完成 ({mode}): "
                f"摘要 {len(results)} 篇 "
                f"({doi_count} DOI, {len(papers) - doi_count} 标题搜索)"
            )

        return results

    def _fetch_one_doi_batch(
        self, dois: list[str], batch_num: int = 1, total_batches: int = 1
    ) -> dict[str, dict]:
        """Fetch a single batch of DOIs via pipe-delimited filter.

        ``GET /works?filter=doi:a|b|...&per_page=200&select=...``
        10 credits per batch (up to 50 DOIs).

        Returns:
            {normalized_doi: {"abstract": str, "doi": str, "title": str,
                              "concepts": list[dict], "referenced_works": list[str]}}
        """
        results: dict[str, dict] = {}
        doi_filter = "|".join(dois)
        batch_label = f"batch-{batch_num}"

        if total_batches > 1:
            print(
                f"  [OpenAlex] 批次 {batch_num}/{total_batches} "
                f"({len(dois)} DOIs)...",
                end=" ", flush=True,
            )

        params = self._build_params(
            filter=f"doi:{doi_filter}",
            per_page=200,
        )
        params["select"] = "id,doi,title,abstract_inverted_index,concepts,referenced_works"

        data = self._fetch_with_retry(
            self.SEARCH_URL, params, label=batch_label
        )
        if data is None:
            if total_batches > 1:
                print("✗ 失败")
            return results

        works = data.get("results", [])
        n_abstracts = sum(
            1 for w in works
            if w.get("abstract_inverted_index")
            and isinstance(w["abstract_inverted_index"], dict)
            and len(w["abstract_inverted_index"]) > 0
        )
        n_concepts = sum(1 for w in works if w.get("concepts"))
        n_refs = sum(len(w.get("referenced_works") or []) for w in works)
        if total_batches > 1:
            print(f"{len(works)} hits | +摘要:{n_abstracts} +主题:{n_concepts} +引用URL:{n_refs}")

        for work in works:
            work_doi = (work.get("doi") or "").strip()
            if not work_doi:
                continue
            # Normalize: OpenAlex returns full URL, strip prefix + lowercase
            if work_doi.startswith("https://doi.org/"):
                work_doi = work_doi[len("https://doi.org/"):]
            work_doi = work_doi.lower()

            # Extract concepts (topics) with scores
            concepts = []
            for c in (work.get("concepts") or []):
                if c and c.get("display_name"):
                    concepts.append({
                        "topic": c["display_name"],
                        "score": c.get("score", 0),
                    })
            concepts.sort(key=lambda x: x["score"], reverse=True)

            # Extract short IDs from full URLs
            ref_ids = [_extract_oa_id(u) for u in (work.get("referenced_works") or [])]

            results[work_doi] = {
                "abstract": self._extract_abstract(work) or "",
                "doi": work_doi,
                "title": work.get("title", "") or "",
                "concepts": concepts,
                "referenced_works": ref_ids,
            }

        return results

    def _resolve_referenced_works(self, db):
        """Resolve OpenAlex short IDs to titles via reference_work.

        1. Query paper_reference for IDs not yet resolved in reference_work
        2. Batch-resolve via OpenAlex API (50 IDs/batch, 10 credits each)
        3. Store in reference_work (resolved=1) or mark not-found (resolved=0)
        """
        total_ids = db.conn.execute(
            """SELECT COUNT(DISTINCT openalex_id) FROM paper_reference
               WHERE openalex_id != '' AND source = 'openalex'"""
        ).fetchone()[0]

        cached = db.conn.execute(
            """SELECT COUNT(*) FROM reference_work WHERE resolved = 1"""
        ).fetchone()[0]

        unresolved = db.get_unresolved_ref_ids()
        skip = total_ids - len(unresolved)

        if not unresolved:
            print(
                f"  [OpenAlex] 参考文献: {total_ids} 个唯一ID, "
                f"全部已解析 ({cached} 篇), 跳过"
            )
            return

        total_batches = (len(unresolved) + self._BATCH_CHUNK_SIZE - 1) // self._BATCH_CHUNK_SIZE
        print(
            f"  [OpenAlex] 解析参考文献:\n"
            f"    paper_reference 唯一ID: {total_ids}\n"
            f"    reference_work 已解析:  {cached} 篇\n"
            f"    去重后待解析:          {len(unresolved)} 篇 (跳过 {skip} 个已缓存)\n"
            f"    批次: {total_batches} (50 IDs/批, 10 credits/批)"
        )

        resolved = 0
        not_found = 0
        for i in range(0, len(unresolved), self._BATCH_CHUNK_SIZE):
            chunk = unresolved[i:i + self._BATCH_CHUNK_SIZE]
            batch_num = i // self._BATCH_CHUNK_SIZE + 1

            # Build full URLs for API call
            id_filter = "|".join(_build_oa_url(oid) for oid in chunk)
            params = self._build_params(
                filter=f"openalex_id:{id_filter}",
                per_page=200,
            )
            params["select"] = "id,title"

            label = f"refs-batch-{batch_num}"
            data = self._fetch_with_retry(self.SEARCH_URL, params, label=label)
            if data is None:
                continue

            found_ids: set[str] = set()
            batch_works = []
            for work in (data.get("results") or []):
                work_url = work.get("id", "")
                title = (work.get("title") or "").strip()
                if work_url and title:
                    oid = _extract_oa_id(work_url)
                    batch_works.append({"openalex_id": oid, "title": title, "resolved": 1})
                    found_ids.add(oid)

            # Mark not-found IDs
            for oid in chunk:
                if oid not in found_ids:
                    batch_works.append({"openalex_id": oid, "title": "", "resolved": 0})
                    not_found += 1

            if batch_works:
                db.save_reference_works(batch_works)
                resolved += len([w for w in batch_works if w["resolved"] == 1])

            n_hits = len([w for w in batch_works if w["resolved"] == 1])
            print(
                f"  [OpenAlex] refs 批次 {batch_num}/{total_batches} "
                f"({len(chunk)} IDs)... {n_hits} hits",
                flush=True,
            )

        print(
            f"  [OpenAlex] 解析完成: 查到 {resolved} 篇, "
            f"未查到 {not_found} 篇"
        )

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
        params.setdefault(
            "select",
            "id,doi,title,abstract_inverted_index,authorships,cited_by_count,"
            "concepts,referenced_works",
        )
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
