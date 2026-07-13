"""Abstract base class for paper fetchers and PaperMeta dataclass."""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class PaperMeta:
    """Paper metadata independent of any specific API source."""

    title: str
    year: int
    authors: list[str] = field(default_factory=list)
    dblp_key: str = ""
    doi: str = ""
    venue: str = ""          # venue key, e.g. "hpca"
    abstract: str = ""
    citation_count: int = 0


@dataclass
class VenueMeta:
    """Venue metadata from config."""

    key: str
    name: str
    type: str               # "conference" | "journal"
    ccf_rank: str
    dblp_venue_key: str
    year_start: int
    year_end: int


class AbstractFetcher(ABC):
    """Base class for paper metadata fetchers."""

    @abstractmethod
    def fetch_papers_by_venue_year(
        self, venue: VenueMeta, year: int
    ) -> list[PaperMeta]:
        """Fetch all papers for a given venue + year."""

    @abstractmethod
    def fetch_abstract(self, paper: PaperMeta) -> str | None:
        """Fetch abstract for a single paper. Returns None if not found."""

    def fetch_abstracts_batch(
        self, papers: list[PaperMeta]
    ) -> dict[str, str]:
        """Fetch abstracts for multiple papers.

        Returns dict mapping dblp_key -> abstract.
        Default implementation calls fetch_abstract sequentially.
        """
        results = {}
        for paper in papers:
            abstract = self.fetch_abstract(paper)
            if abstract:
                results[paper.dblp_key] = abstract
        return results
