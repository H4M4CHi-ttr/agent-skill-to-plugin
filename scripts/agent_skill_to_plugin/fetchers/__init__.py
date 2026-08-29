"""Safe source acquisition primitives."""

from .archive import ArchiveExtractionResult, extract_archive
from .http import HttpFetcher, HttpFetchResult, validate_public_https_url
from .npm import NpmFetcher, NpmFetchResult

__all__ = [
    "ArchiveExtractionResult",
    "HttpFetcher",
    "HttpFetchResult",
    "NpmFetcher",
    "NpmFetchResult",
    "extract_archive",
    "validate_public_https_url",
]
