from datetime import UTC
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import get_object_or_404

from documents.caching import CACHE_5_MINUTES
from documents.caching import CACHE_50_MINUTES
from documents.caching import CLASSIFIER_HASH_KEY
from documents.caching import CLASSIFIER_MODIFIED_KEY
from documents.caching import CLASSIFIER_VERSION_KEY
from documents.caching import get_thumbnail_modified_key
from documents.classifier import DocumentClassifier
from documents.models import Document
from documents.versioning import resolve_requested_version


def suggestions_etag(request, pk: int) -> str | None:
    """
    Returns an optional string for the ETag, allowing browser caching of
    suggestions if the classifier has not been changed and the suggested dates
    setting is also unchanged

    """
    # If no model file, no etag at all
    if not settings.MODEL_FILE.exists():
        return None
    # Check cache information
    cache_hits = cache.get_many(
        [CLASSIFIER_VERSION_KEY, CLASSIFIER_HASH_KEY],
    )
    # If the version differs somehow, no etag
    if (
        CLASSIFIER_VERSION_KEY in cache_hits
        and cache_hits[CLASSIFIER_VERSION_KEY] != DocumentClassifier.FORMAT_VERSION
    ):
        return None
    elif CLASSIFIER_HASH_KEY in cache_hits:
        # Refresh the cache and return the hash digest and the dates setting
        cache.touch(CLASSIFIER_HASH_KEY, CACHE_5_MINUTES)
        return f"{cache_hits[CLASSIFIER_HASH_KEY]}:{settings.NUMBER_OF_SUGGESTED_DATES}"
    return None


def suggestions_last_modified(request, pk: int) -> datetime | None:
    """
    Returns the datetime of classifier last modification.  This is slightly off,
    as there is not way to track the suggested date setting modification, but it seems
    unlikely that changes too often
    """
    # No file, no last modified
    if not settings.MODEL_FILE.exists():
        return None
    cache_hits = cache.get_many(
        [CLASSIFIER_VERSION_KEY, CLASSIFIER_MODIFIED_KEY],
    )
    # If the version differs somehow, no last modified
    if (
        CLASSIFIER_VERSION_KEY in cache_hits
        and cache_hits[CLASSIFIER_VERSION_KEY] != DocumentClassifier.FORMAT_VERSION
    ):
        return None
    elif CLASSIFIER_MODIFIED_KEY in cache_hits:
        # Refresh the cache and return the last modified
        cache.touch(CLASSIFIER_MODIFIED_KEY, CACHE_5_MINUTES)
        return cache_hits[CLASSIFIER_MODIFIED_KEY]
    return None


def metadata_etag(request, pk: int) -> str | None:
    """
    Metadata is extracted from the original file, so use its checksum as the
    ETag
    """
    doc = get_object_or_404(Document, pk=pk)
    resolution = resolve_requested_version(doc, request)
    version = resolution.version
    if version is None:
        return None
    return version.checksum


def metadata_last_modified(request, pk: int) -> datetime | None:
    """
    Metadata is extracted from the original file, so use its added time.
    """
    doc = get_object_or_404(Document, pk=pk)
    resolution = resolve_requested_version(doc, request)
    version = resolution.version
    if version is None:
        return None
    return version.added


def preview_etag(request, pk: int) -> str | None:
    """
    ETag for the document preview, using the original or archive checksum, depending on the request
    """
    doc = get_object_or_404(Document, pk=pk)
    resolution = resolve_requested_version(doc, request)
    version = resolution.version
    if version is None:
        return None
    use_original = (
        hasattr(request, "query_params")
        and "original" in request.query_params
        and request.query_params["original"] == "true"
    )
    return version.checksum if use_original else version.archive_checksum


def preview_last_modified(request, pk: int) -> datetime | None:
    """
    Uses the version added time to set the Last-Modified header.
    """
    doc = get_object_or_404(Document, pk=pk)
    resolution = resolve_requested_version(doc, request)
    version = resolution.version
    if version is None:
        return None
    return version.added


def thumbnail_last_modified(request: Any, pk: int) -> datetime | None:
    """
    Returns the filesystem last modified either from cache or from filesystem.
    Cache should be (slightly?) faster than filesystem
    """
    try:
        doc = get_object_or_404(Document, pk=pk)
        resolution = resolve_requested_version(doc, request)
        version = resolution.version
        if version is None:
            return None
        if not version.thumbnail_path.exists():
            return None
        doc_key = get_thumbnail_modified_key(version.id)

        cache_hit = cache.get(doc_key)
        if cache_hit is not None:
            cache.touch(doc_key, CACHE_50_MINUTES)
            return cache_hit

        last_modified = datetime.fromtimestamp(
            version.thumbnail_path.stat().st_mtime,
            tz=UTC,
        )
        cache.set(doc_key, last_modified, CACHE_50_MINUTES)
        return last_modified
    except (Document.DoesNotExist, OSError):  # pragma: no cover
        return None
