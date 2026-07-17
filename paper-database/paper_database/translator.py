"""LLM translator: async httpx calls for English → Chinese abstract translation.

Reuses the same OpenAI-compatible API infrastructure as the classifier
(config, httpx client, semaphore, retry logic).  Translation is a simpler
task — single prompt, plain text output, no JSON parsing needed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional, Callable

import httpx

from paper_database.config import ClassifierConfig
from paper_database.db import Database


TRANSLATION_PROMPT = """Translate the following academic paper abstract from English to Chinese.
Output ONLY the Chinese translation — no explanations, no notes, no markdown formatting.

Title: {title}

Abstract:
{abstract}"""


class Translator:
    """Async translator using LLM API for abstract → Chinese translation."""

    def __init__(
        self, config: ClassifierConfig, max_concurrency: int | None = None
    ):
        self.api_base_url = config.api_base_url.rstrip("/")
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.temperature = 0.0  # translation is deterministic
        self.max_concurrency = max_concurrency or config.max_concurrency
        self.timeout = config.timeout
        self.max_retries = config.max_retries
        self.strip_fence = config.strip_markdown_fence

        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError(
                "API key not configured. "
                "请在 config/classifier.yaml 的 providers 中设置 api_key，"
                "或使用 {env:VAR_NAME} 引用环境变量。"
            )

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

    async def translate_abstract(self, title: str, abstract: str) -> str:
        """Translate a single abstract. Returns Chinese text."""
        prompt = self._build_prompt(title, abstract)
        return await self._call_api(prompt)

    async def translate_papers(
        self,
        db: Database,
        limit: int | None = None,
        progress_callback: Callable[[int, int, str, str], None] | None = None,
    ) -> tuple[int, int]:
        """Translate all papers in the main database that need it.

        Returns (translated, failed).
        """
        return await self._run_translation(
            db=db,
            get_papers_fn=lambda l: db.get_papers_needing_translation(l),
            update_fn=lambda dblp_key, text: db.update_paper_abstract_cn(
                dblp_key, text
            ),
            limit=limit,
            progress_callback=progress_callback,
        )

    async def translate_survey_papers(
        self,
        db: Database,
        survey_id: int,
        limit: int | None = None,
        progress_callback: Callable[[int, int, str, str], None] | None = None,
    ) -> tuple[int, int]:
        """Translate selected (include=1) papers in a survey DB.

        Returns (translated, failed).
        """
        return await self._run_translation(
            db=db,
            get_papers_fn=lambda l: db.get_survey_papers_needing_translation(
                survey_id, l
            ),
            update_fn=lambda dblp_key, text: db.update_paper_abstract_cn(
                dblp_key, text
            ),
            limit=limit,
            progress_callback=progress_callback,
        )

    async def close(self):
        """Release the HTTP client."""
        await self._client.aclose()

    # ── Internals ────────────────────────────────────────────

    def _build_prompt(self, title: str, abstract: str) -> str:
        return TRANSLATION_PROMPT.format(title=title, abstract=abstract)

    async def _call_api(self, prompt: str) -> str:
        """Call chat completions API. Returns cleaned response text.

        Guarded by a global semaphore to cap total concurrent API calls.
        """
        messages = [{"role": "user", "content": prompt}]
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
            "response_format": {"type": "text"},
        }

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
                    wait = 2 ** (attempt + 2)  # 4, 8, 16, 32s
                    last_error = e
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(wait)
                elif 500 <= status < 600:
                    last_error = e
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                else:
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
        """Remove markdown code fences (``` ... ```) from output."""
        text = text.strip()
        text = re.sub(r"^```(?:[a-zA-Z]+)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    async def _run_translation(
        self,
        db: Database,
        get_papers_fn,
        update_fn,
        limit: int | None = None,
        progress_callback: (
            Callable[[int, int, str, str], None] | None
        ) = None,
    ) -> tuple[int, int]:
        """Generic Feeder/Worker translation pipeline.

        Feeder reads papers in batches and puts them into a queue.
        Workers pull from the queue, call the API, and write results.

        Args:
            db: Database instance.
            get_papers_fn: Callable(limit) → list[dict] of papers.
            update_fn: Callable(dblp_key, text) → None.
            limit: Max papers to translate (None = all).
            progress_callback: Called with (done, total, title, status).

        Returns:
            (translated_count, failed_count)
        """
        BATCH_SIZE = 500
        queue: asyncio.Queue[dict | None] = asyncio.Queue(
            maxsize=self.max_concurrency * 2
        )

        translated = 0
        failed = 0
        total_to_translate = 0

        # ── Feeder ───────────────────────────────────────────
        async def _feeder():
            nonlocal total_to_translate
            remaining = limit if limit is not None else None
            total_fed = 0

            while True:
                batch_limit = min(BATCH_SIZE, remaining) if remaining is not None else BATCH_SIZE
                batch = get_papers_fn(batch_limit)
                if not batch:
                    break

                if total_to_translate == 0 and batch:
                    # First batch — try to get total count for progress
                    pass

                for paper in batch:
                    await queue.put(paper)
                    total_fed += 1
                    if remaining is not None:
                        remaining -= 1
                        if remaining <= 0:
                            break

                if remaining is not None and remaining <= 0:
                    break
                if len(batch) < BATCH_SIZE:
                    break

            total_to_translate = total_fed
            # Signal workers to stop
            for _ in range(self.max_concurrency):
                await queue.put(None)

        # ── Worker ───────────────────────────────────────────
        async def _worker(worker_id: int):
            nonlocal translated, failed
            while True:
                paper = await queue.get()
                try:
                    if paper is None:
                        return

                    title = paper.get("title", "")
                    abstract = paper.get("abstract", "")
                    dblp_key = paper.get("dblp_key", "")

                    if not abstract.strip():
                        queue.task_done()
                        continue

                    try:
                        cn_text = await self.translate_abstract(title, abstract)
                        if cn_text:
                            update_fn(dblp_key, cn_text)
                            translated += 1
                            if progress_callback:
                                progress_callback(
                                    translated + failed,
                                    total_to_translate,
                                    title,
                                    "translated",
                                )
                        else:
                            failed += 1
                            if progress_callback:
                                progress_callback(
                                    translated + failed,
                                    total_to_translate,
                                    title,
                                    "empty_response",
                                )
                    except RuntimeError as e:
                        failed += 1
                        if progress_callback:
                            progress_callback(
                                translated + failed,
                                total_to_translate,
                                title,
                                f"failed: {e}",
                            )
                finally:
                    queue.task_done()

        # ── Run ──────────────────────────────────────────────
        try:
            feeder = asyncio.create_task(_feeder())
            workers = [
                asyncio.create_task(_worker(i))
                for i in range(self.max_concurrency)
            ]
            await feeder
            await asyncio.gather(*workers)
        finally:
            await self._client.aclose()

        return translated, failed
