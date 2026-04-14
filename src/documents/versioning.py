from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any

from documents.models import Document
from documents.models import DocumentVersion

if TYPE_CHECKING:
    from django.http import HttpRequest


class VersionResolutionError(StrEnum):
    INVALID = "invalid"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class VersionResolution:
    version: DocumentVersion | None
    error: VersionResolutionError | None = None


def get_request_version_param(request: HttpRequest) -> str | None:
    if hasattr(request, "query_params"):
        return request.query_params.get("version")
    return None


def get_latest_version(doc: Document) -> DocumentVersion | None:
    """Return the highest-version_number DocumentVersion for doc, or None."""
    return (
        DocumentVersion.objects.filter(document=doc).order_by("-version_number").first()
    )


def get_version_by_pk(doc: Document, version_pk: int) -> DocumentVersion | None:
    """Return the DocumentVersion with the given pk if it belongs to doc."""
    return DocumentVersion.objects.filter(pk=version_pk, document=doc).first()


def resolve_requested_version(
    doc: Document,
    request: Any,
) -> VersionResolution:
    """
    Resolve the DocumentVersion to serve based on the optional ``?version=<pk>``
    query parameter.

    - No parameter: return the latest version.
    - Parameter present: validate and return that specific version.
    """
    version_param = get_request_version_param(request)
    if not version_param:
        latest = get_latest_version(doc)
        if latest is None:
            return VersionResolution(
                version=None,
                error=VersionResolutionError.NOT_FOUND,
            )
        return VersionResolution(version=latest)

    try:
        version_pk = int(version_param)
    except (TypeError, ValueError):
        return VersionResolution(version=None, error=VersionResolutionError.INVALID)

    version = get_version_by_pk(doc, version_pk)
    if version is None:
        return VersionResolution(version=None, error=VersionResolutionError.NOT_FOUND)
    return VersionResolution(version=version)
