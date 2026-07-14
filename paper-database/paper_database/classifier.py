"""DeepSeek API classifier: async httpx calls with configurable concurrency.

Replaces the old subprocess-based CLIClassifier.  Supports
deepseek-chat and deepseek-reasoner models, with optional thinking
mode.  Concurrency is controlled via asyncio.Semaphore (default 32).
"""

from __future__ import annotations

import asyncio
import json
import re
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
            limits=httpx.Limits(
                max_connections=self.max_concurrency + 10,
                max_keepalive_connections=self.max_concurrency + 10,
            ),
        )

    # ── Public API ───────────────────────────────────────────

    async def classify_single(
        self, paper: PaperMeta, topic: TopicConfig
    ) -> ClassificationResult:
        """Classify a single paper. Returns ClassificationResult."""
        prompt = self._build_prompt(paper, topic)
        output = await self._call_api(prompt)
        return self._parse_response(output)

    async def run_survey(
        self,
        db: Database,
        survey_id: int,
        topic: TopicConfig,
        dry_run: bool = False,
        limit: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ):
        """Run classification for all unclassified papers in a survey.

        Claim-based Queue + Worker pattern:

        1. On startup, reset any 'claimed' flags to 'unclaimed' (crash recovery).
        2. Feeder atomically claims papers (SELECT + UPDATE in transaction)
           only when the in-memory queue has room (max 2× concurrency).
        3. N workers pull, classify, save result, then mark paper 'classified'.

        This eliminates the old race condition where papers still in the
        queue could be re-fetched by a subsequent get_unclassified() call.
        """
        total = 0
        n = self.max_concurrency
        max_inflight = n * 2  # memory cap: 2× concurrency

        # ── Crash recovery: reset stale claimed flags ──────────
        db.reset_claimed_flags()

        try:
            # ── dry-run: just print prompts ────────────────────
            if dry_run:
                while True:
                    rows = db.claim_papers(survey_id, limit=2000)
                    if not rows:
                        break
                    for row in rows:
                        total += 1
                        paper = PaperMeta(
                            title=row["title"], year=row["year"],
                            authors=json.loads(row["authors"]),
                            dblp_key=row["dblp_key"],
                            abstract=row.get("abstract", "") or "",
                        )
                        prompt = self._build_prompt(paper, topic)
                        print(f"\n{'='*60}")
                        print(f"[DRY RUN] Paper: {paper.title[:80]}...")
                        print(f"Venue: {row.get('venue_name','')} ({row['year']})")
                        print(f"Prompt:\n{prompt}")
                        print(f"{'='*60}")
                        if limit and total >= limit:
                            return
                        # Release claim for dry-run (reset to unclaimed)
                        db.conn.execute(
                            "UPDATE paper SET flag = 'unclaimed' WHERE id = ?",
                            (row["paper_id"],),
                        )
                        db.conn.commit()
                return

            # ── Queue + Workers ─────────────────────────────────
            queue: asyncio.Queue = asyncio.Queue(maxsize=max_inflight)
            done = asyncio.Event()
            lock = asyncio.Lock()
            all_claimed = asyncio.Event()  # set when no more papers to claim

            async def _worker():
                """Pull paper from queue, classify, save, mark classified."""
                while True:
                    item = await queue.get()
                    if item is None:          # sentinel — pass to next worker
                        await queue.put(None)
                        break

                    paper, row = item

                    # ── Classify (with immediate retry) ─────────
                    result = None
                    for retry in range(3):
                        try:
                            result = await self.classify_single(paper, topic)
                            break
                        except Exception:
                            if retry < 2:
                                await asyncio.sleep(2 ** (retry + 1))
                    if result is None:
                        # All retries failed — paper stays 'claimed' in DB
                        # but will be reset to 'unclaimed' on next restart
                        queue.task_done()
                        continue

                    # ── Save result + mark paper classified ─────
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
                    # Mark paper flag as classified LAST
                    db.mark_paper_classified(row["paper_id"])

                    # ── Progress ─────────────────────────────────
                    async with lock:
                        nonlocal total
                        total += 1
                        if progress_callback:
                            progress_callback(total, 0, paper.title, result)
                        if limit and total >= limit:
                            done.set()

                    queue.task_done()

            # ── Feeder: claim papers on demand, push to queue ────
            async def _feeder():
                while not done.is_set():
                    # Don't claim more than queue can hold
                    room = max_inflight - queue.qsize()
                    if room <= 0:
                        await asyncio.sleep(0.05)
                        continue

                    batch = min(room, 500)  # claim at most 500 at a time
                    rows = db.claim_papers(survey_id, batch)
                    if not rows:
                        # No more unclaimed papers — feed what's left,
                        # then send sentinels
                        break

                    for row in rows:
                        paper = PaperMeta(
                            title=row["title"], year=row["year"],
                            authors=json.loads(row["authors"]),
                            dblp_key=row["dblp_key"],
                            doi=row.get("doi", "") or "",
                            venue=row.get("venue_key", ""),
                            abstract=row.get("abstract", "") or "",
                            citation_count=row.get("citation_count", 0),
                        )
                        await queue.put((paper, dict(row)))
                        if done.is_set():
                            return

                # All papers claimed — send sentinels to workers
                await queue.put(None)

            # ── Launch feeder + workers concurrently ────────────
            workers = [asyncio.create_task(_worker()) for _ in range(n)]
            feeder = asyncio.create_task(_feeder())
            await feeder
            await asyncio.gather(*workers)

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
                # JSON parse failed — treat as classification failure.
                # Raise so the worker retries the API call; if all
                # retries fail the paper stays 'claimed' and will be
                # reset to 'unclaimed' on next restart.
                raise ValueError(f"[JSON parse failed] {text[:200]}")

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
