"""DBLP API client — fetch paper lists by venue + year.

Uses DBLP XML exports for complete, accurate paper lists.
XML export avoids the search API's substring-matching issues
(e.g., venue:MICRO matching thousands of false positives).

URL discovery: parses each venue's index.xml to find the actual
XML export URLs. This handles:
  - Multi-volume conferences (ASPLOS 2023 has 4 volumes)
  - Name changes (FPT → ICFPT from 2021)
  - Journal volume numbers (tc → tc65.xml etc.)

Conference pattern: https://dblp.org/db/conf/{abbrev}/index.xml
Journal pattern:   https://dblp.org/db/journals/{abbrev}/index.xml
"""

from __future__ import annotations

import re
import time
from typing import Optional
from xml.etree import ElementTree

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class DBLPFetcher(AbstractFetcher):
    """Fetches paper metadata from DBLP XML exports.

    First downloads the venue's index.xml to discover available XML files,
    then downloads only those matching the target years. No API key required.
    """

    BASE_URL = "https://dblp.org/db"

    def __init__(self, timeout: float = 30.0, rate_limit: float = 0.5):
        self.timeout = timeout
        self.rate_limit = rate_limit
        self._headers = {
            "User-Agent": "paper-database/0.2 (academic literature tool)"
        }
        # Cache: venue.key → {year: [xml_url_suffix, ...]}
        self._url_cache: dict[str, dict[int, list[str]]] = {}

    # ── Public API ─────────────────────────────────────────────

    def discover_year_urls(self, venue: VenueMeta) -> dict[int, list[str]]:
        """Discover XML URLs grouped by year from the venue's index.xml.

        Returns {year: [url_suffix, ...]} where url_suffix is like
        "conf/asplos/asplos2023-1" (without .xml extension).
        Multi-volume conferences return multiple URLs per year.

        Retries with exponential backoff on transient errors.
        Does NOT cache failures — only successful results are cached.
        """
        if venue.key in self._url_cache:
            return self._url_cache[venue.key]

        index_url = f"{self.BASE_URL}/{venue.dblp_url_prefix}/index.xml"
        result = self._fetch_index_with_retry(index_url, venue.key)

        if result is not None:
            self._url_cache[venue.key] = result
            return result

        # Failure: don't cache, return empty dict
        return {}

    def _fetch_index_with_retry(
        self, url: str, venue_key: str, max_retries: int = 3
    ) -> Optional[dict[int, list[str]]]:
        """Fetch and parse index.xml with exponential backoff retry.

        Returns None on persistent failure (so caller can distinguish
        "index has no entries" from "index fetch failed").
        """
        last_error = ""
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 2 ** attempt  # 2s, 4s, 8s
                print(
                    f"  [DBLP] 重试 {venue_key} index ({attempt + 1}/{max_retries}), "
                    f"等待 {wait}s..."
                )
                time.sleep(wait)

            try:
                response = httpx.get(
                    url, headers=self._headers, timeout=self.timeout
                )
                response.raise_for_status()

                # Parse based on venue type (we don't have VenueMeta here,
                # but we can check the URL prefix)
                if "/journals/" in url:
                    return self._parse_journal_index(response.text, None)
                else:
                    return self._parse_conference_index(response.text, None)

            except httpx.HTTPStatusError as e:
                # Don't retry on 404 — the index genuinely doesn't exist
                if e.response.status_code == 404:
                    print(f"  [DBLP] No index for {venue_key} (404)")
                    return None
                last_error = str(e)
            except Exception as e:
                last_error = str(e)

        print(f"  [DBLP] Error fetching index for {venue_key}: {last_error}")
        return None

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int, urls: Optional[list[str]] = None
    ) -> tuple[list[PaperMeta], list[str]]:
        """Fetch all papers from a venue in a specific year.

        If `urls` is provided, only fetch those specific XML URL suffixes.
        Otherwise, discover URLs from index.xml.

        Returns (papers, fetched_urls) — fetched_urls lists the URL suffixes
        that were successfully downloaded.
        """
        if urls is not None:
            url_list = urls
        else:
            year_urls = self.discover_year_urls(venue)
            url_list = year_urls.get(year, [])

            if not url_list:
                # Fall back to legacy URL pattern
                legacy_url = self._build_legacy_url(venue, year)
                if legacy_url:
                    url_list = [legacy_url]

        all_papers: list[PaperMeta] = []
        fetched: list[str] = []

        for i, url_suffix in enumerate(url_list):
            if len(url_list) > 1:
                print(f"  卷{i+1}...", end=" ", flush=True)
            full_url = f"{self.BASE_URL}/{url_suffix}.xml"

            if i > 0:
                time.sleep(self.rate_limit)

            xml_text = self._fetch_xml_with_retry(full_url, venue.key)
            if xml_text is None:
                continue

            papers = self._parse_xml(xml_text, venue, year)
            all_papers.extend(papers)
            fetched.append(url_suffix)

        return all_papers, fetched

    def _fetch_xml_with_retry(
        self, url: str, venue_key: str, max_retries: int = 3
    ) -> Optional[str]:
        """获取 XML 内容，带指数退避重试。404 不重试。"""
        last_error = ""
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 2 ** attempt  # 2s, 4s, 8s
                print(
                    f"  [DBLP] 重试 {venue_key} ({attempt + 1}/{max_retries}), "
                    f"等待 {wait}s..."
                )
                time.sleep(wait)

            try:
                response = httpx.get(
                    url, headers=self._headers, timeout=self.timeout,
                )
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                last_error = str(e)
            except Exception as e:
                last_error = str(e)

        print(f"  [DBLP] Error fetching {url}: {last_error}")
        return None

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """DBLP does not provide abstracts. Always returns None."""
        return None

    # ── Index parsing ──────────────────────────────────────────

    def _parse_conference_index(
        self, xml_text: str, venue: VenueMeta = None
    ) -> dict[int, list[str]]:
        """Parse conference index.xml to discover proceedings XML URLs.

        Extracts <proceedings> entries, groups by year.
        Year is parsed from the URL filename (e.g. asplos2023-1 → 2023),
        NOT from the <year> element (which may reflect publication date
        rather than conference year for multi-volume proceedings).
        """
        year_urls: dict[int, list[str]] = {}

        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return year_urls

        for proc in root.iter("proceedings"):
            url_elem = proc.find("url")
            if url_elem is None or not url_elem.text:
                continue

            url_path = url_elem.text.strip()
            if url_path.endswith(".html"):
                url_path = url_path[:-5]
            if url_path.startswith("db/"):
                url_path = url_path[3:]

            # Extract year from URL filename, e.g.:
            # "conf/asplos/asplos2023-1" → 2023
            # "conf/hpca/hpca2016" → 2016
            # "conf/fpt/icfpt2021" → 2021
            filename = url_path.split("/")[-1]
            year_match = re.search(r"(20\d{2})", filename)
            if not year_match:
                continue
            year = int(year_match.group(1))

            if year not in year_urls:
                year_urls[year] = []
            if url_path not in year_urls[year]:
                year_urls[year].append(url_path)

        return year_urls

    def _parse_journal_index(
        self, xml_text: str, venue: VenueMeta = None
    ) -> dict[int, list[str]]:
        """Parse journal index.xml to discover volume XML URLs.

        Extracts <ref href="...">Volume XX, YYYY</ref> entries,
        maps year to volume XML URL.
        """
        year_urls: dict[int, list[str]] = {}

        # Journal index uses <ref href="db/journals/tc/tc75.html">Volume 75, 2026</ref>
        # We need to parse volume number and year, then convert to XML URL
        for match in re.finditer(
            r'<ref\s+href="([^"]+\.html)"[^>]*>([^<]+)</ref>',
            xml_text,
        ):
            url = match.group(1)
            text = match.group(2)

            # Extract year from text like "Volume 75, 2026" or "Volume 45: 2026"
            year_match = re.search(r"(\d{4})\s*$", text.strip())
            if not year_match:
                continue
            year = int(year_match.group(1))

            # Convert HTML URL to XML URL suffix
            if url.startswith("db/"):
                url = url[3:]
            if url.endswith(".html"):
                url = url[:-5]

            if year not in year_urls:
                year_urls[year] = []
            if url not in year_urls[year]:
                year_urls[year].append(url)

        return year_urls

    # ── Legacy URL (fallback) ──────────────────────────────────

    def _build_legacy_url(
        self, venue: VenueMeta, year: int
    ) -> Optional[str]:
        """Build a URL using the legacy pattern. Returns None for journals
        (journals never worked with the legacy pattern anyway)."""
        if venue.type == "journal":
            return None

        prefix = venue.dblp_url_prefix  # e.g. "conf/hpca"
        abbrev = prefix.split("/")[-1]  # "hpca"

        # Standard pattern
        url = f"{prefix}/{abbrev}{year}"
        return url

    # ── XML parsing ────────────────────────────────────────────

    def _parse_xml(
        self, xml_text: str, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """Parse DBLP XML and extract papers for the given year."""
        papers: list[PaperMeta] = []

        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as e:
            print(f"  [DBLP] XML parse error for {venue.key} {year}: {e}")
            return papers

        # Conference: <inproceedings>, Journal: <article>
        # DBLP XML nests papers inside <dblpcites>/<r>/<inproceedings>,
        # so we must recursively search, not just iterate root children.
        for child in root.iter():
            if child.tag not in ("inproceedings", "article"):
                continue

            # For journals, filter by year
            child_year_elem = child.find("year")
            if child_year_elem is not None:
                try:
                    child_year = int(child_year_elem.text or "0")
                except ValueError:
                    continue
                if child_year != year:
                    continue

            title_elem = child.find("title")
            if title_elem is None or not title_elem.text:
                continue

            title = title_elem.text.strip()

            # Extract authors
            authors = [
                a.text.strip()
                for a in child.findall("author")
                if a.text
            ]

            # Extract DOI from <ee> elements
            doi = ""
            for ee in child.findall("ee"):
                if ee.text and "doi" in ee.text.lower():
                    doi = ee.text.strip()
                    break

            # DBLP key
            dblp_key = child.get("key", "")

            papers.append(PaperMeta(
                title=title,
                year=year,
                authors=authors,
                dblp_key=dblp_key,
                doi=doi,
                venue=venue.key,
            ))

        return papers
