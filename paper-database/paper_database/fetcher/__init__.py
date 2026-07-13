"""Fetcher module — DBLP, Semantic Scholar, OpenAlex API clients."""

from paper_database.fetcher.base import AbstractFetcher, PaperMeta
from paper_database.fetcher.dblp import DBLPFetcher
from paper_database.fetcher.semantic_scholar import SemanticScholarFetcher
from paper_database.fetcher.openalex import OpenAlexFetcher

__all__ = [
    "AbstractFetcher",
    "PaperMeta",
    "DBLPFetcher",
    "SemanticScholarFetcher",
    "OpenAlexFetcher",
]
