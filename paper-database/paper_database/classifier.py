"""DeepSeek API classifier: async httpx calls with configurable concurrency.

Replaces the old subprocess-based CLIClassifier.  Supports
deepseek-chat and deepseek-reasoner models, with optional thinking
mode.  Concurrency is controlled via asyncio.Semaphore (default 32).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass
from typing import Optional, Callable

import httpx

from paper_database.config import ClassifierConfig, TopicConfig
from paper_database.db import Database
from paper_database.fetcher.base import PaperMeta


@dataclass
class ClassificationResult:
    priority: str = ""             # "S" / "A" / "B" / "" ("" = not relevant)
    reason: str = ""
    confidence: float = 0.0
    # Structured extraction fields
    research_object: str = ""      # 研究对象
    problem_goal: str = ""         # 问题/目标
    method_innovation: str = ""    # 方法/创新
    algorithm: str = ""            # 调度算法

    @property
    def is_relevant(self) -> bool:
        """Backward compat: True if any priority is set."""
        return self.priority in ("S", "A", "B")


class DeepSeekClassifier:
    """Async classifier using direct DeepSeek API calls via httpx."""

    def __init__(self, config: ClassifierConfig):
        self.api_base_url = config.api_base_url.rstrip("/")
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.temperature = config.temperature
        self.enable_thinking = config.enable_thinking
        self.max_concurrency = config.max_concurrency
        self.timeout = config.timeout
        self.max_retries = config.max_retries
        self.strip_fence = config.strip_markdown_fence

        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError(
                f"{config.provider} API key not configured.\n"
                "请在 config/classifier.yaml 的 providers 中设置 api_key，"
                "或使用 {env:VAR_NAME} 引用环境变量。\n"
                "配置示例: api_key: \"{env:API_KEY}\" 或 api_key: \"sk-your-key-here\""
            )

        self._client = httpx.AsyncClient(
            base_url=self.api_base_url,
            timeout=httpx.Timeout(self.timeout),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self._sem = asyncio.Semaphore(self.max_concurrency)

    # ── Public API ───────────────────────────────────────────

    async def classify_single(
        self, paper: PaperMeta, topic: TopicConfig
    ) -> ClassificationResult:
        """Classify a single paper. Returns ClassificationResult."""
        prompt = self._build_prompt(paper, topic)
        output = await self._call_api(prompt)
        return self._parse_response(output)

    async def classify_batch(
        self,
        papers: list[PaperMeta],
        topic: TopicConfig,
        progress_callback: Optional[Callable] = None,
        save_callback: Optional[Callable] = None,
    ) -> list[ClassificationResult]:
        """Classify multiple papers concurrently (bounded by semaphore).

        If ``save_callback`` is provided, it is called for each paper
        immediately after classification completes (survives Ctrl+C).
        """
        completed = 0
        total = len(papers)
        lock = asyncio.Lock()

        async def _process_one(paper: PaperMeta) -> ClassificationResult:
            nonlocal completed
            last_error = None
            result = None
            for retry in range(3):
                try:
                    async with self._sem:
                        result = await self.classify_single(paper, topic)
                    break
                except Exception as e:
                    last_error = e
                    if retry < 2:
                        wait = 2 ** (retry + 1)  # 2s, 4s
                        await asyncio.sleep(wait)
            if result is None:
                result = ClassificationResult(
                    priority="",
                    reason=f"[API error after 3 retries] {last_error}",
                    confidence=0.0,
                )
            else:
                if save_callback:
                    save_callback(paper, result)
            async with lock:
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, paper.title, result)
            return result

        tasks = [_process_one(p) for p in papers]
        return await asyncio.gather(*tasks)

    async def run_survey(
        self,
        db: Database,
        survey_id: int,
        topic: TopicConfig,
        dry_run: bool = False,
        limit: Optional[int] = None,
        start: int = 1,
        progress_callback: Optional[Callable] = None,
        abstract_fetcher=None,
        papers_db: Optional[Database] = None,
    ):
        """Run classification for all unclassified papers in a survey.

        Args:
            db: Survey Database instance (classification results).
            survey_id: Survey ID to process.
            topic: Topic configuration.
            dry_run: If True, print prompts without calling API.
            limit: Max papers to classify (None = all).
            start: Skip first (start-1) papers (for resume).
            progress_callback: Called after each classification.
            abstract_fetcher: Optional AbstractFetcher.
            papers_db: Main papers Database (for reading/writing abstracts).
                Defaults to ``db`` if not provided.
        """
        batch_size = 50
        total_processed = 0
        skipped = 0

        _abstract_db = papers_db or db

        # ── Background thread: fetch ALL missing abstracts ──────
        if abstract_fetcher is not None and papers_db is not None:
            all_rows = papers_db.get_papers_without_abstract(limit=999999)
            if all_rows:
                all_missing = [
                    PaperMeta(
                        title=r["title"], year=r["year"],
                        authors=json.loads(r.get("authors", "[]")),
                        dblp_key=r["dblp_key"],
                        doi=r.get("doi", "") or "",
                        abstract=r.get("abstract", "") or "",
                    )
                    for r in all_rows
                ]
                bg_db_path = papers_db.db_path
                print(
                    f"  [后台] 启动摘要预取线程 ({len(all_missing)} 篇, "
                    f"{sum(1 for p in all_missing if p.doi.strip())} 有 DOI)..."
                )

                def _fetch_all():
                    bg_db = Database(bg_db_path)
                    bg_db.init_db()
                    abstract_fetcher.fetch_abstracts_batch(
                        all_missing, db=bg_db, doi_only=True,
                    )

                threading.Thread(target=_fetch_all, daemon=True).start()

        try:
            while True:
                rows = db.get_unclassified(survey_id, limit=batch_size)
                if not rows:
                    break

                # Build paper list for this batch, applying resume offset
                papers: list[PaperMeta] = []
                row_index: dict[str, dict] = {}  # dblp_key → DB row

                for row in rows:
                    skipped += 1
                    if skipped < start:
                        continue

                    paper = PaperMeta(
                        title=row["title"],
                        year=row["year"],
                        authors=json.loads(row["authors"]),
                        dblp_key=row["dblp_key"],
                        doi=row.get("doi", "") or "",
                        venue=row.get("venue_key", ""),
                        abstract=row.get("abstract", "") or "",
                        citation_count=row.get("citation_count", 0),
                    )
                    papers.append(paper)
                    row_index[paper.dblp_key] = row

                if not papers:
                    # All rows in this batch were skipped — keep going
                    if len(rows) < batch_size:
                        break
                    continue

                if dry_run:
                    for paper in papers:
                        total_processed += 1
                        prompt = self._build_prompt(paper, topic)
                        print(f"\n{'='*60}")
                        print(f"[DRY RUN] Paper: {paper.title[:80]}...")
                        print(
                            f"Venue: {row_index[paper.dblp_key].get('venue_name','')}"
                            f" ({paper.year})"
                        )
                        print(f"Prompt:\n{prompt}")
                        print(f"{'='*60}")
                        if limit and total_processed >= limit:
                            return
                    continue

                # ── Check papers_db for existing abstracts ─────────
                # survey DB 的 paper 表是快照，需查主库确认是否有摘要
                if _abstract_db is not db:
                    for paper in papers:
                        if not paper.abstract.strip():
                            existing = _abstract_db.get_paper_abstract(paper.dblp_key)
                            if existing:
                                paper.abstract = existing

                # ── 逐篇保存回调 ──────────────────────────────────
                def _save(paper: PaperMeta, result: ClassificationResult):
                    row = row_index.get(paper.dblp_key, {})
                    analysis_json = ""
                    if result.is_relevant and any([
                        result.research_object, result.problem_goal,
                        result.method_innovation, result.algorithm,
                    ]):
                        analysis_json = json.dumps({
                            "priority": result.priority,
                            "research_object": result.research_object,
                            "problem_goal": result.problem_goal,
                            "method_innovation": result.method_innovation,
                            "algorithm": result.algorithm,
                        }, ensure_ascii=False)
                    db.mark_result(
                        row.get("result_id", 0),
                        is_relevant=result.priority,
                        reason=result.reason,
                        confidence=result.confidence,
                        analysis_json=analysis_json,
                    )

                # ── Split ready / missing ──────────────────────────
                ready = [p for p in papers if p.abstract.strip()]
                missing = [p for p in papers if not p.abstract.strip()]
                batch_done = 0

                # Classify papers that have abstracts
                if ready:
                    await self.classify_batch(
                        ready, topic,
                        progress_callback=progress_callback,
                        save_callback=_save,
                    )
                    batch_done += len(ready)

                # Missing papers
                if missing:
                    if abstract_fetcher is not None:
                        # Give background thread a chance to write abstracts
                        await asyncio.sleep(1.0)
                        for paper in missing:
                            abstract = _abstract_db.get_paper_abstract(paper.dblp_key)
                            if abstract:
                                paper.abstract = abstract
                        retry_ready = [p for p in missing if p.abstract.strip()]
                        if retry_ready:
                            await self.classify_batch(
                                retry_ready, topic,
                                progress_callback=progress_callback,
                                save_callback=_save,
                            )
                            batch_done += len(retry_ready)
                        # Still-missing papers: skip (is_relevant stays NULL → retry next batch)
                    else:
                        # No abstract fetcher: classify as-is (标题判断)
                        await self.classify_batch(
                            missing, topic,
                            progress_callback=progress_callback,
                            save_callback=_save,
                        )
                        batch_done += len(missing)

                total_processed += batch_done

                # Stall detection: if nothing got classified this batch,
                # the background thread may have finished or we have no
                # abstracts for the remaining papers — stop.
                if batch_done == 0:
                    print(
                        "  [classify] 本批 0 篇可分类（等待摘要后台线程），"
                        "停止。重新运行可接续。"
                    )
                    break

                if limit and total_processed >= limit:
                    return

                if len(rows) < batch_size:
                    break
        finally:
            await self._client.aclose()

    def _build_prompt(self, paper: PaperMeta, topic: TopicConfig) -> str:
        abstract = paper.abstract or "（无摘要，仅根据标题判断）"
        return topic.prompt_template.format(
            topic_name=topic.name,
            topic_description=topic.description,
            topic_keywords=", ".join(topic.keywords),
            title=paper.title,
            abstract=abstract,
        )

    async def _call_api(self, prompt: str) -> str:
        """Call DeepSeek chat completions API. Returns JSON string from content."""
        messages = [{"role": "user", "content": prompt}]
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

        # deepseek-v4-pro supports both temperature and thinking mode
        body["temperature"] = self.temperature

        if self.enable_thinking:
            body["thinking"] = {"type": "enabled"}

        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = await self._client.post(
                    "/v1/chat/completions",
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if self.strip_fence:
                    content = self._strip_markdown_fence(content)
                return content.strip()

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 429:
                    # Rate limited — longer backoff
                    wait = 2 ** (attempt + 2)  # 4, 8, 16, 32s
                    last_error = e
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(wait)
                elif 500 <= status < 600:
                    # Server error — standard backoff
                    last_error = e
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                else:
                    # 4xx — no point retrying
                    raise RuntimeError(
                        f"API error {status}: {e.response.text[:500]}"
                    ) from e

            except (httpx.TimeoutException, httpx.RequestError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"API call failed after {self.max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        """Remove markdown code fences (```json ... ```) from output."""
        text = text.strip()
        # Remove opening fence
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        # Remove closing fence
        text = re.sub(r"\n?```\s*$", "", text)
        # Also handle ``` without newlines
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    @staticmethod
    def _parse_response(text: str) -> ClassificationResult:
        """Parse JSON response from API."""
        # Try to find JSON object in the text
        json_match = re.search(r'\{[^{}]*"priority"[^{}]*\}', text, re.DOTALL)
        if json_match:
            text = json_match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to fix common issues
            text = text.replace("'", '"')
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Last resort: check for S/A/B in raw text
                priority = ""
                for p in ("S", "A", "B"):
                    if p in text.upper():
                        priority = p
                        break
                return ClassificationResult(
                    priority=priority,
                    reason=f"[JSON parse failed] {text[:200]}",
                    confidence=0.3,
                )

        priority = str(data.get("priority", "") or "")
        # Normalize: accept "S"/"A"/"B", reject anything else
        if priority not in ("S", "A", "B"):
            priority = ""

        return ClassificationResult(
            priority=priority,
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.5)),
            research_object=str(data.get("research_object", "")),
            problem_goal=str(data.get("problem_goal", "")),
            method_innovation=str(data.get("method_innovation", "")),
            algorithm=str(data.get("algorithm", "")),
        )
