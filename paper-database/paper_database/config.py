"""Configuration loader: YAML files → Python dataclasses."""

import os
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
    prompt_template: str = ""
    output: OutputConfig = field(default_factory=OutputConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "TopicConfig":
        return cls(
            key=d["key"],
            name=d["name"],
            description=d.get("description", ""),
            keywords=d.get("keywords", []),
            prompt_template=d.get("prompt_template", ""),
            output=OutputConfig.from_dict(d.get("output", {})),
        )


def _resolve_env(value: str) -> str:
    """Resolve ``{env:VAR_NAME}`` placeholders in a config string value.

    Returns the literal value if no placeholder is found, otherwise
    substitutes the referenced environment variable.  If the variable
    is not set the placeholder is left as-is so the downstream code
    can raise a clear error.
    """
    import re as _re

    def _replace(match: _re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return _re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", _replace, value)


@dataclass
class ProviderConfig:
    """Provider-specific settings (api_base_url, api_key, model, etc.)."""

    api_base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-v4-pro"
    max_tokens: int = 500
    temperature: float = 0.0
    enable_thinking: bool = False
    max_concurrency: int = 32

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderConfig":
        return cls(
            api_base_url=d.get("api_base_url", "https://api.deepseek.com"),
            api_key=_resolve_env(d.get("api_key", "")),
            model=d.get("model", "deepseek-v4-pro"),
            max_tokens=d.get("max_tokens", 500),
            temperature=d.get("temperature", 0.0),
            enable_thinking=d.get("enable_thinking", False),
            max_concurrency=d.get("max_concurrency", 32),
        )


@dataclass
class DeliberationConfig:
    """Multi-round deliberation settings for improving classification reliability."""

    enabled: bool = False
    rounds: int = 3             # odd number recommended (3, 5, 7)
    strategy: str = "majority"  # "majority" | "supermajority" | "consensus"
    temperature_override: float = 0.0  # 0.0 = use classifier default temperature
    supermajority_ratio: float = 0.67  # for supermajority strategy

    @classmethod
    def from_dict(cls, d: dict) -> "DeliberationConfig":
        if not d:
            return cls()
        return cls(
            enabled=d.get("enabled", False),
            rounds=d.get("rounds", 3),
            strategy=d.get("strategy", "majority"),
            temperature_override=d.get("temperature_override", 0.0),
            supermajority_ratio=d.get("supermajority_ratio", 0.67),
        )


@dataclass
class ClassifierConfig:
    provider: str = "deepseek"
    api_base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-v4-pro"
    max_tokens: int = 500
    temperature: float = 0.0
    enable_thinking: bool = False
    max_concurrency: int = 32
    timeout: int = 60
    max_retries: int = 3
    strip_markdown_fence: bool = True
    deliberation: DeliberationConfig = field(default_factory=DeliberationConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "ClassifierConfig":
        provider_name = d.get("provider", "deepseek")

        # Load provider-specific settings
        providers_dict = d.get("providers", {})
        if providers_dict:
            # New format: read from providers.<name>
            provider_data = dict(providers_dict.get(provider_name, {}))
            # Top-level values serve as defaults for per-provider overrides
            for key in ("max_concurrency",):
                if key not in provider_data:
                    provider_data[key] = d.get(key, 32)
            provider = ProviderConfig.from_dict(provider_data)
        else:
            # Backward compat: old flat format
            provider = ProviderConfig.from_dict(d)

        return cls(
            provider=provider_name,
            api_base_url=provider.api_base_url,
            api_key=provider.api_key,
            model=provider.model,
            max_tokens=provider.max_tokens,
            temperature=provider.temperature,
            enable_thinking=provider.enable_thinking,
            max_concurrency=provider.max_concurrency,
            timeout=d.get("timeout", 60),
            max_retries=d.get("max_retries", 3),
            strip_markdown_fence=d.get("strip_markdown_fence", True),
            deliberation=DeliberationConfig.from_dict(d.get("deliberation", {})),
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
