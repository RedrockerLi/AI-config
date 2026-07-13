"""DBLP API client — fetch paper lists by venue + year.

Uses DBLP XML exports for complete, accurate paper lists.
XML export avoids the search API's substring-matching issues
(e.g., venue:MICRO matching thousands of false positives).

Conference pattern: https://dblp.org/db/conf/{abbrev}/{abbrev}{year}.xml
Journal pattern:   https://dblp.org/db/journals/{abbrev}/{abbrev}.xml
"""

from typing import Optional
from xml.etree import ElementTree

import httpx

from paper_database.fetcher.base import AbstractFetcher, PaperMeta, VenueMeta


class DBLPFetcher(AbstractFetcher):
    """Fetches paper metadata from DBLP XML exports.

    No API key required. XML exports provide authoritative, complete
    paper lists without the 1000-result cap of the search API.
    """

    BASE_URL = "https://dblp.org/db"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._headers = {
            "User-Agent": "paper-database/0.1 (academic literature tool)"
        }

    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """Fetch all papers from a venue in a specific year.

        Uses DBLP XML export (no false positives from substring matching).
        """
        url = self._build_url(venue, year)
        try:
            response = httpx.get(
                url, headers=self._headers, timeout=self.timeout
            )
            response.raise_for_status()
        except Exception as e:
            print(f"  [DBLP] Error fetching {venue.key} {year}: {e}")
            return []

        return self._parse_xml(response.text, venue, year)

    def fetch_abstract(self, paper: PaperMeta) -> Optional[str]:
        """DBLP does not provide abstracts. Always returns None."""
        return None

    # ── Internal ─────────────────────────────────────────────

    def _build_url(self, venue: VenueMeta, year: int) -> str:
        prefix = venue.dblp_url_prefix  # e.g. "conf/hpca" or "journals/tc"
        if venue.type == "conference":
            abbrev = prefix.split("/")[-1]  # "hpca" from "conf/hpca"
            return f"{self.BASE_URL}/{prefix}/{abbrev}{year}.xml"
        else:
            # Journals: one XML per journal containing ALL years.
            # We fetch the single file and filter by year in _parse_xml.
            abbrev = prefix.split("/")[-1]
            return f"{self.BASE_URL}/{prefix}/{abbrev}.xml"

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
