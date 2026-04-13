from __future__ import annotations

from types import SimpleNamespace

import pytest

from documents.tests.factories import DocumentFactory
from documents.tests.factories import DocumentVersionFactory
from documents.versioning import VersionResolutionError
from documents.versioning import get_latest_version
from documents.versioning import get_version_by_pk
from documents.versioning import resolve_requested_version


@pytest.mark.django_db
class TestGetLatestVersion:
    def test_returns_highest_version_number(self) -> None:
        doc = DocumentFactory()
        DocumentVersionFactory(document=doc, version_number=1)
        DocumentVersionFactory(document=doc, version_number=2)
        v3 = DocumentVersionFactory(document=doc, version_number=3)
        result = get_latest_version(doc)
        assert result is not None
        assert result.pk == v3.pk

    def test_returns_none_when_no_versions(self) -> None:
        doc = DocumentFactory()
        assert get_latest_version(doc) is None


@pytest.mark.django_db
class TestGetVersionByPk:
    def test_returns_version_belonging_to_document(self) -> None:
        doc = DocumentFactory()
        v = DocumentVersionFactory(document=doc, version_number=1)
        result = get_version_by_pk(doc, v.pk)
        assert result is not None
        assert result.pk == v.pk

    def test_returns_none_for_unrelated_version(self) -> None:
        doc_a = DocumentFactory()
        doc_b = DocumentFactory()
        v_b = DocumentVersionFactory(document=doc_b, version_number=1)
        assert get_version_by_pk(doc_a, v_b.pk) is None

    def test_returns_none_for_nonexistent_pk(self) -> None:
        doc = DocumentFactory()
        assert get_version_by_pk(doc, 999999) is None


@pytest.mark.django_db
class TestResolveRequestedVersion:
    def test_no_version_param_returns_latest(self) -> None:
        doc = DocumentFactory()
        DocumentVersionFactory(document=doc, version_number=1)
        v2 = DocumentVersionFactory(document=doc, version_number=2)
        request = SimpleNamespace(query_params={})
        result = resolve_requested_version(doc, request)
        assert result.version is not None
        assert result.version.pk == v2.pk
        assert result.error is None

    def test_explicit_version_param_returns_that_version(self) -> None:
        doc = DocumentFactory()
        v1 = DocumentVersionFactory(document=doc, version_number=1)
        DocumentVersionFactory(document=doc, version_number=2)
        request = SimpleNamespace(query_params={"version": str(v1.pk)})
        result = resolve_requested_version(doc, request)
        assert result.version is not None
        assert result.version.pk == v1.pk

    def test_invalid_version_param_returns_error(self) -> None:
        doc = DocumentFactory()
        request = SimpleNamespace(query_params={"version": "notanint"})
        result = resolve_requested_version(doc, request)
        assert result.version is None
        assert result.error == VersionResolutionError.INVALID

    def test_unrelated_version_id_returns_not_found(self) -> None:
        doc_a = DocumentFactory()
        doc_b = DocumentFactory()
        v_b = DocumentVersionFactory(document=doc_b, version_number=1)
        request = SimpleNamespace(query_params={"version": str(v_b.pk)})
        result = resolve_requested_version(doc_a, request)
        assert result.version is None
        assert result.error == VersionResolutionError.NOT_FOUND
