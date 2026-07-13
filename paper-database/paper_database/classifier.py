"""CLI-based classifier: subprocess calls local LLM CLI tool.

Supports any CLI tool (claude, ollama, tgpt, etc.) configured via
config/classifier.yaml.  Parses JSON output from the tool.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Callable

from paper_database.config import ClassifierConfig, TopicConfig
from paper_database.db import Database
from paper_database.fetcher.base import PaperMeta


@dataclass
class ClassificationResult:
    is_relevant: bool
    reason: str
    confidence: float
    # Structured extraction fields (populated when is_relevant=True)
    research_object: str = ""      # 研究对象
    problem_goal: str = ""         # 问题/目标
    method_innovation: str = ""    # 方法/创新
    algorithm: str = ""            # 调度算法


class CLIClassifier:
    """Calls a local CLI LLM tool via subprocess to classify papers."""

    def __init__(self, config: ClassifierConfig):
        self.tool = config.tool
        self.cli_args = config.cli_args
        self.prompt_template = config.prompt_template
        self.delay = config.delay_seconds
        self.timeout = config.timeout
        self.max_retries = config.max_retries
        self.strip_fence = config.strip_markdown_fence

    # ── Public API ───────────────────────────────────────────

    def classify_single(
        self, paper: PaperMeta, topic: TopicConfig
    ) -> ClassificationResult:
        """Classify a single paper. Returns ClassificationResult."""
        prompt = self._build_prompt(paper, topic)
        output = self._call_cli(prompt)
        return self._parse_response(output)

    def classify_batch(
        self,
        papers: list[PaperMeta],
        topic: TopicConfig,
        progress_callback: Optional[Callable] = None,
    ) -> list[ClassificationResult]:
        """Classify multiple papers with rate limiting."""
        results = []
        for i, paper in enumerate(papers):
            result = self.classify_single(paper, topic)
            results.append(result)
            if progress_callback:
                progress_callback(i + 1, len(papers), paper.title, result)
            time.sleep(self.delay)
        return results

    def run_survey(
        self,
        db: Database,
        survey_id: int,
        topic: TopicConfig,
        dry_run: bool = False,
        limit: Optional[int] = None,
        start: int = 1,
        progress_callback: Optional[Callable] = None,
    ):
        """Run classification for all unclassified papers in a survey.

        Args:
            db: Database instance.
            survey_id: Survey ID to process.
            topic: Topic configuration.
            dry_run: If True, print prompts without calling CLI.
            limit: Max papers to classify (None = all).
            start: Skip first (start-1) papers (for resume).
            progress_callback: Called after each classification.
        """
        batch_size = 50  # fetch from DB per batch
        total_processed = 0
        skipped = 0

        while True:
            unclassified = db.get_unclassified(survey_id, limit=batch_size)
            if not unclassified:
                break

            for row in unclassified:
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

                if dry_run:
                    total_processed += 1
                    prompt = self._build_prompt(paper, topic)
                    print(f"\n{'='*60}")
                    print(f"[DRY RUN] Paper: {paper.title[:80]}...")
                    print(f"Venue: {row.get('venue_name','')} ({paper.year})")
                    print(f"Prompt:\n{prompt}")
                    print(f"{'='*60}")
                    if limit and total_processed >= limit:
                        return
                    continue

                # Real classification
                result = self.classify_single(paper, topic)
                analysis_json = ""
                if result.is_relevant and any([
                    result.research_object,
                    result.problem_goal,
                    result.method_innovation,
                    result.algorithm,
                ]):
                    analysis_json = json.dumps({
                        "研究对象": result.research_object,
                        "问题/目标": result.problem_goal,
                        "方法/创新": result.method_innovation,
                        "调度算法": result.algorithm,
                    }, ensure_ascii=False)
                db.mark_result(
                    row["result_id"],
                    result.is_relevant,
                    result.reason,
                    result.confidence,
                    analysis_json=analysis_json,
                )

                total_processed += 1
                if progress_callback:
                    progress_callback(
                        total_processed, None, paper.title, result
                    )

                if limit and total_processed >= limit:
                    return

            # If we got fewer than batch_size, we're done
            if len(unclassified) < batch_size:
                break

    # ── Internal helpers ─────────────────────────────────────

    def _build_prompt(self, paper: PaperMeta, topic: TopicConfig) -> str:
        abstract = paper.abstract or "（无摘要，仅根据标题判断）"
        return self.prompt_template.format(
            topic_name=topic.name,
            topic_description=topic.description,
            topic_keywords=", ".join(topic.keywords),
            title=paper.title,
            abstract=abstract,
        )

    def _call_cli(self, prompt: str) -> str:
        """Call the CLI tool with the given prompt. Returns stdout."""
        args = [arg.format(prompt=prompt) for arg in self.cli_args]
        cmd = [self.tool] + args

        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                output = result.stdout

                # If stdout is empty, try stderr
                if not output.strip():
                    output = result.stderr

                if self.strip_fence:
                    output = self._strip_markdown_fence(output)

                return output.strip()

            except subprocess.TimeoutExpired as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)

        raise RuntimeError(
            f"CLI call failed after {self.max_retries} attempts: {last_error}"
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
        """Parse JSON response from CLI tool."""
        # Try to find JSON object in the text
        json_match = re.search(r'\{[^{}]*"is_relevant"[^{}]*\}', text, re.DOTALL)
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
                # Last resort: keyword matching
                lower = text.lower()
                is_rel = (
                    "true" in lower
                    and '"is_relevant"' in lower
                    and not '"is_relevant": false' in lower
                )
                return ClassificationResult(
                    is_relevant=is_rel,
                    reason=f"[JSON parse failed] {text[:200]}",
                    confidence=0.3,
                )

        return ClassificationResult(
            is_relevant=bool(data.get("is_relevant", False)),
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.5)),
            research_object=str(data.get("研究对象", "")),
            problem_goal=str(data.get("问题/目标", "")),
            method_innovation=str(data.get("方法/创新", "")),
            algorithm=str(data.get("调度算法", "")),
        )
