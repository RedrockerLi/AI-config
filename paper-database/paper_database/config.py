"""Configuration loader: YAML files → Python dataclasses."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ── Config dataclasses ──────────────────────────────────────────

@dataclass
class VenueConfig:
    key: str
    name: str
    type: str                  # "conference" | "journal"
    ccf_rank: str              # "A" | "B" | "C"
    dblp_url_prefix: str       # DBLP URL path: "conf/hpca" or "journals/tc"
    year_start: int
    year_end: int

    @classmethod
    def from_dict(cls, d: dict) -> "VenueConfig":
        return cls(
            key=d["key"],
            name=d["name"],
            type=d["type"],
            ccf_rank=d.get("ccf_rank", ""),
            dblp_url_prefix=d["dblp_url_prefix"],
            year_start=d.get("year_start", 2016),
            year_end=d.get("year_end", 2026),
        )


@dataclass
class OutputColumn:
    field: str
    header: str
    width: int = 20
    transform: str = ""       # "join_comma" | "bool_to_yes_no" | "percent" | ""

    @classmethod
    def from_dict(cls, d: dict) -> "OutputColumn":
        return cls(
            field=d["field"],
            header=d.get("header", d["field"]),
            width=d.get("width", 20),
            transform=d.get("transform", ""),
        )


@dataclass
class OutputConfig:
    format: str = "xlsx"      # "xlsx" | "csv"
    sort_by: list[str] = field(default_factory=lambda: ["year", "venue_name"])
    columns: list[OutputColumn] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "OutputConfig":
        cols = [OutputColumn.from_dict(c) for c in d.get("columns", [])]
        return cls(
            format=d.get("format", "xlsx"),
            sort_by=d.get("sort_by", ["year", "venue_name"]),
            columns=cols,
        )


@dataclass
class TopicConfig:
    key: str
    name: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    output: OutputConfig = field(default_factory=OutputConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "TopicConfig":
        return cls(
            key=d["key"],
            name=d["name"],
            description=d.get("description", ""),
            keywords=d.get("keywords", []),
            output=OutputConfig.from_dict(d.get("output", {})),
        )


@dataclass
class ClassifierConfig:
    provider: str = "deepseek"
    api_base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    prompt_template: str = ""
    max_tokens: int = 500
    temperature: float = 0.0
    enable_thinking: bool = False
    max_concurrency: int = 32
    timeout: int = 60
    max_retries: int = 3
    strip_markdown_fence: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "ClassifierConfig":
        return cls(
            provider=d.get("provider", "deepseek"),
            api_base_url=d.get("api_base_url", "https://api.deepseek.com"),
            model=d.get("model", "deepseek-chat"),
            prompt_template=d.get("prompt_template", ""),
            max_tokens=d.get("max_tokens", 500),
            temperature=d.get("temperature", 0.0),
            enable_thinking=d.get("enable_thinking", False),
            max_concurrency=d.get("max_concurrency", 32),
            timeout=d.get("timeout", 60),
            max_retries=d.get("max_retries", 3),
            strip_markdown_fence=d.get("strip_markdown_fence", True),
        )


# ── Config loader ───────────────────────────────────────────────

class Config:
    """Top-level config holder. Loads from config/ directory."""

    def __init__(self, config_dir: str | Path = "config"):
        self.config_dir = Path(config_dir)
        self.venues: list[VenueConfig] = []
        self.topics: list[TopicConfig] = []
        self.classifier: ClassifierConfig = ClassifierConfig()
        self._loaded = False

    def load(self) -> "Config":
        """Load all config files from config_dir."""
        self.venues = self._load_venues()
        self.topics = self._load_topics()
        self.classifier = self._load_classifier()
        self._loaded = True
        return self

    def _load_yaml(self, filename: str) -> dict:
        path = self.config_dir / filename
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_venues(self) -> list[VenueConfig]:
        data = self._load_yaml("venues.yaml")
        return [VenueConfig.from_dict(v) for v in data.get("venues", [])]

    def _load_topics(self) -> list[TopicConfig]:
        data = self._load_yaml("topics.yaml")
        return [TopicConfig.from_dict(t) for t in data.get("topics", [])]

    def _load_classifier(self) -> ClassifierConfig:
        data = self._load_yaml("classifier.yaml")
        return ClassifierConfig.from_dict(data.get("classifier", {}))

    def get_topic(self, key: str) -> Optional[TopicConfig]:
        for t in self.topics:
            if t.key == key:
                return t
        return None

    def get_venue(self, key: str) -> Optional[VenueConfig]:
        for v in self.venues:
            if v.key == key:
                return v
        return None


# Singleton-like convenience
_config: Optional[Config] = None


def get_config(config_dir: str | Path = "config") -> Config:
    global _config
    if _config is None:
        _config = Config(config_dir).load()
    return _config


def reload_config(config_dir: str | Path = "config") -> Config:
    global _config
    _config = Config(config_dir).load()
    return _config
