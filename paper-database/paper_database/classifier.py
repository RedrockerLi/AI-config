"""LLM classifier: async httpx calls with configurable concurrency.

Replaces the old subprocess-based CLIClassifier.  Supports any
OpenAI-compatible API (DeepSeek, OpenAI, etc.), with optional
thinking mode.  Concurrency is controlled via asyncio.Semaphore
(default 32).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Callable

import httpx

from paper_database.config import ClassifierConfig, DeliberationConfig, TopicConfig
from paper_database.db import Database
from paper_database.fetcher.base import PaperMeta


@dataclass
class ClassificationResult:
    """`include` is the only hardcoded field — set from model JSON ``include`` key.

    1 = paper belongs in survey, 0 = does not.  Stored in DB ``include``.
    Everything else goes into ``extra``, written to real DB columns.
    """

    include: int = 0  # 1 = include in survey, 0 = exclude
    extra: dict = field(default_factory=dict)


class LLMClassifier:
    """Async classifier using LLM API calls (OpenAI-compatible) via httpx."""

    def __init__(self, config: ClassifierConfig, deliberation: Optional[DeliberationConfig] = None):
        self.api_base_url = config.api_base_url.rstrip("/")
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.temperature = config.temperature
        self.enable_thinking = config.enable_thinking
        self.max_concurrency = config.max_concurrency
        self.timeout = config.timeout
        self.max_retries = config.max_retries
        self.strip_fence = config.strip_markdown_fence

        # Deliberation config: param overrides file, file overrides default
        self.deliberation = deliberation or config.deliberation

        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError(
                f"{config.provider} API key not configured.\n"
                "请在 config/classifier.yaml 的 providers 中设置 api_key，"
                "或使用 {env:VAR_NAME} 引用环境变量。\n"
                "配置示例: api_key: \"{env:API_KEY}\" 或 api_key: \"sk-your-key-here\""
            )

        # Global semaphore caps total concurrent API calls across all workers.
        # Essential when deliberation multiplies calls per paper.
        self._api_semaphore = asyncio.Semaphore(self.max_concurrency)

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

    async def debug_classify_single(
        self, paper: PaperMeta, topic: TopicConfig,
        deliberation_rounds: int = 0,
    ) -> tuple[str, str, ClassificationResult, list[dict] | None]:
        """Classify a single paper and return (prompt, raw_response, result, rounds).

        For debugging: does NOT save to DB — caller prints results to stdout.

        If ``deliberation_rounds`` > 1, runs N parallel rounds and returns
        the aggregated result plus per-round details for display.
        """
        prompt = self._build_prompt(paper, topic)

        if deliberation_rounds > 1:
            result, round_details = await self.classify_with_deliberation(
                paper, topic, return_details=True,
            )
            return prompt, "", result, round_details

        output = await self._call_api(prompt)
        result = self._parse_response(output)
        return prompt, output, result, None

    # ── Deliberation (multi-round voting) ─────────────────────

    async def classify_with_deliberation(
        self, paper: PaperMeta, topic: TopicConfig,
        return_details: bool = False,
    ) -> ClassificationResult | tuple[ClassificationResult, list[dict]]:
        """Run N parallel classification rounds and aggregate via voting.

        Args:
            paper: The paper to classify.
            topic: Survey topic config (prompt template, etc.).
            return_details: If True, also return per-round debug info.

        Returns:
            ClassificationResult if ``return_details=False``,
            else (ClassificationResult, list[dict]) with per-round details.
        """
        n = self.deliberation.rounds

        # Run N rounds in parallel
        tasks = [self._classify_single_round(paper, topic, i) for i in range(n)]
        round_results = await asyncio.gather(*tasks)

        # Separate successes from failures
        successes = [r for r in round_results if r is not None]
        if not successes:
            raise RuntimeError(
                f"Deliberation: all {n} rounds failed for '{paper.title[:60]}'"
            )

        # Aggregate
        result = self._aggregate_results(
            [r["result"] for r in successes],
            strategy=self.deliberation.strategy,
        )

        if return_details:
            return result, round_results
        return result

    async def _classify_single_round(
        self, paper: PaperMeta, topic: TopicConfig, round_idx: int,
    ) -> dict | None:
        """Run one classification round. Returns debug dict or None on failure.

        Uses deliberation temperature override if configured (> 0).
        Each round uses the existing worker-level retry (3 attempts).
        """
        temp_override = self.deliberation.temperature_override
        for retry in range(3):
            try:
                prompt = self._build_prompt(paper, topic)
                output = await self._call_api(prompt, temperature_override=temp_override)
                result = self._parse_response(output)
                return {
                    "round": round_idx + 1,
                    "prompt": prompt,
                    "raw_response": output,
                    "result": result,
                }
            except Exception:
                if retry < 2:
                    await asyncio.sleep(2 ** (retry + 1))
        return None

    def _aggregate_results(
        self,
        results: list[ClassificationResult],
        strategy: str = "majority",
    ) -> ClassificationResult:
        """Aggregate multiple ClassificationResults via voting.

        - ``include``: strategy-based voting (majority/supermajority/consensus).
        - Categorical extra fields: most common value among the winning group.
        - Free-text extra fields: value from the first winning-group result.

        Confidence metadata (_deliberation_confidence, _deliberation_rounds)
        is appended to ``extra``.
        """
        n = len(results)
        if n == 0:
            return ClassificationResult(include=0)

        if n == 1:
            r = results[0]
            r.extra["_deliberation_confidence"] = "1/1"
            r.extra["_deliberation_rounds"] = "1"
            return r

        include_votes = [r.include for r in results]
        include_count = sum(include_votes)
        exclude_count = n - include_count

        # ── Determine include via strategy ─────────────────
        if strategy == "consensus":
            if include_count == n:
                include = 1
            elif include_count == 0:
                include = 0
            else:
                include = -1  # uncertain — treated as exclude but flagged
        elif strategy == "supermajority":
            ratio = self.deliberation.supermajority_ratio
            include = 1 if include_count / n >= ratio else 0
        else:  # majority (default)
            # Majority: > N/2 wins; ties → include ("宁可多收录")
            include = 1 if include_count > n / 2 else 0

        # ── Build winning group ────────────────────────────
        if include == -1:
            # consensus deadlock: use all results
            winning = results
        elif include == 1:
            winning = [r for r in results if r.include == 1]
            if not winning:
                winning = results  # fallback for supermajority edge case
        else:
            winning = [r for r in results if r.include == 0]
            if not winning:
                winning = results

        # ── Aggregate extra fields ─────────────────────────
        extra: dict = {}

        # Collect all keys across results
        all_keys = set()
        for r in winning:
            all_keys.update(r.extra.keys())

        # Skip internal metadata keys when aggregating
        internal_keys = {"_deliberation_confidence", "_deliberation_rounds"}

        for key in sorted(all_keys - internal_keys):
            values = [r.extra.get(key, "") for r in winning if r.extra.get(key, "")]
            if not values:
                # Also check losing group
                values = [r.extra.get(key, "") for r in results if r.extra.get(key, "")]
            if values:
                # Most common value
                extra[key] = Counter(values).most_common(1)[0][0]
            else:
                extra[key] = ""

        # ── Confidence metadata ────────────────────────────
        extra["_deliberation_confidence"] = f"{include_count}/{n}"
        extra["_deliberation_rounds"] = str(n)

        return ClassificationResult(include=include, extra=extra)

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
                fed = 0
                while True:
                    want = 2000
                    if limit:
                        want = min(want, limit - fed)
                    rows = db.claim_papers(survey_id, limit=want)
                    if not rows:
                        break
                    for row in rows:
                        total += 1
                        fed += 1
                        paper = PaperMeta(
                            title=row["title"], year=row["year"],
                            authors=json.loads(row["authors"]),
                            dblp_key=row["dblp_key"],
                            abstract=row.get("abstract", "") or "",
                            topics=db.get_paper_topics(row["paper_id"]),
                            references=db.get_paper_references(row["paper_id"]),
                        )
                        prompt = self._build_prompt(paper, topic)
                        print(f"\n{'='*60}")
                        print(f"[DRY RUN] Paper: {paper.title[:80]}...")
                        print(f"Venue: {row.get('venue_name','')} ({row['year']})")
                        print(f"Prompt:\n{prompt}")
                        print(f"{'='*60}")
                        # Release claim for dry-run (reset to unclaimed)
                        db.conn.execute(
                            "UPDATE paper SET flag = 'unclaimed' WHERE id = ?",
                            (row["paper_id"],),
                        )
                        db.conn.commit()
                        if limit and fed >= limit:
                            break
                    if limit and fed >= limit:
                        break
                return

            # ── Queue + Workers ─────────────────────────────────
            queue: asyncio.Queue = asyncio.Queue(maxsize=max_inflight)
            lock = asyncio.Lock()

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
                    if self.deliberation.enabled:
                        # Multi-round deliberation: N parallel calls + voting
                        for retry in range(3):
                            try:
                                result = await self.classify_with_deliberation(
                                    paper, topic,
                                )
                                break
                            except Exception:
                                if retry < 2:
                                    await asyncio.sleep(2 ** (retry + 1))
                    else:
                        # Single-round classification
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
                    db.mark_result(
                        row.get("result_id", 0),
                        include=result.include,
                        columns=result.extra,
                    )
                    # Mark paper flag as classified LAST
                    db.mark_paper_classified(row["paper_id"])

                    # ── Progress ─────────────────────────────────
                    async with lock:
                        nonlocal total
                        total += 1
                        if progress_callback:
                            progress_callback(total, 0, paper.title, result)

                    queue.task_done()

            # ── Feeder: claim papers on demand, push to queue ────
            async def _feeder():
                fed = 0
                while True:
                    room = max_inflight - queue.qsize()
                    if room <= 0:
                        await asyncio.sleep(0.05)
                        continue
                    want = min(room, 500)
                    if limit:
                        want = min(want, limit - fed)
                    if want <= 0:
                        break
                    rows = db.claim_papers(survey_id, want)
                    if not rows:
                        break
                    for row in rows:
                        paper_id = row["paper_id"]
                        paper = PaperMeta(
                            title=row["title"], year=row["year"],
                            authors=json.loads(row["authors"]),
                            dblp_key=row["dblp_key"],
                            doi=row.get("doi", "") or "",
                            venue=row.get("venue_key", ""),
                            abstract=row.get("abstract", "") or "",
                            citation_count=row.get("citation_count", 0),
                            topics=db.get_paper_topics(paper_id),
                            references=db.get_paper_references(paper_id),
                        )
                        await queue.put((paper, dict(row)))
                        fed += 1
                        if limit and fed >= limit:
                            break

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

        # Format keywords: semicolon-separated or placeholder
        keywords_str = "; ".join(paper.topics) if paper.topics else "（无）"

        # Format references: numbered list or placeholder (max 20)
        if paper.references:
            refs_str = "\n".join(
                f"  - {t}" for t in paper.references[:20]
            )
        else:
            refs_str = "  （无）"

        body = topic.prompt_template.format(
            topic_name=topic.name,
            topic_description=topic.description,
            topic_keywords=", ".join(topic.keywords),
            title=paper.title,
            abstract=abstract,
            keywords=keywords_str,
            references=refs_str,
        )
        # System instruction: the ONE hardcoded contract between code and model.
        # The model MUST output "include": true/false so the code knows whether
        # to include this paper in the survey results.
        system = (
            'Your response must be a single JSON object.\n'
            'It MUST include this key:\n'
            '  "include": true if the paper belongs in this survey, false otherwise.\n'
        )
        return system + "\n" + body

    async def _call_api(self, prompt: str, temperature_override: float = 0.0) -> str:
        """Call chat completions API. Returns JSON string from content.

        Guarded by a global semaphore to cap total concurrent API calls
        across all workers (essential when deliberation multiplies calls).

        Args:
            prompt: The full prompt to send.
            temperature_override: If > 0, use this temperature instead of default.
        """
        messages = [{"role": "user", "content": prompt}]
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "text"},
        }

        # deepseek-v4-pro supports both temperature and thinking mode
        body["temperature"] = temperature_override if temperature_override > 0 else self.temperature

        if self.enable_thinking:
            body["thinking"] = {"type": "enabled"}

        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                async with self._api_semaphore:
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
        """Parse JSON response from API.

        The ONE hardcoded contract: the model MUST output ``"include": true/false``.
        Everything else goes into ``extra``, written to survey_result columns.
        """
        # Try to find JSON object containing "include" key
        json_match = re.search(r'\{[^{}]*"include"[^{}]*\}', text, re.DOTALL)
        if json_match:
            text = json_match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            text = text.replace("'", '"')
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise ValueError(f"[JSON parse failed] {text[:200]}")

        # Hardcoded: "include" → include (1/0)
        include_raw = data.pop("include", False)
        include = 1 if include_raw is True or str(include_raw).strip().lower() == "true" else 0

        # Everything else → extra → written to survey_result columns
        extra: dict = {k: str(v) if v else "" for k, v in data.items()}

        return ClassificationResult(include=include, extra=extra)
