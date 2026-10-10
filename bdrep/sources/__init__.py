"""Source registry. Adding a source = one adapter module + one entry here."""

from __future__ import annotations

from bdrep.sources.base import FetchResult, Source, SourceError
from bdrep.sources.news_rss import NewsRssSource
from bdrep.sources.sam_gov import SamGovSource

REGISTRY = {
    SamGovSource.name: SamGovSource,
    NewsRssSource.name: NewsRssSource,
}

__all__ = ["REGISTRY", "FetchResult", "Source", "SourceError", "SamGovSource", "NewsRssSource"]
