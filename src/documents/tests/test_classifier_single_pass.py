"""
Phase 2 — Single queryset pass in DocumentClassifier.train()

The document queryset must be iterated exactly once: during the label
extraction loop, which now also captures doc.content for vectorization.
The previous content_generator() caused a second full table scan.
"""

from __future__ import annotations

from unittest import mock

import pytest
from django.db.models.query import QuerySet

from documents.classifier import DocumentClassifier
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import MatchingModel
from documents.models import StoragePath
from documents.models import Tag

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_classifier_train_skip.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def classifier_settings(settings, tmp_path):
    settings.MODEL_FILE = tmp_path / "model.pickle"
    return settings


@pytest.fixture()
def classifier(classifier_settings):
    return DocumentClassifier()


@pytest.fixture()
def label_corpus(classifier_settings):
    c_auto = Correspondent.objects.create(
        name="Auto Corp",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    dt_auto = DocumentType.objects.create(
        name="Invoice",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    t_auto = Tag.objects.create(
        name="finance",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    sp_auto = StoragePath.objects.create(
        name="Finance Path",
        path="finance/{correspondent}",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )

    doc_a = Document.objects.create(
        title="Invoice A",
        content="quarterly invoice payment tax financial statement revenue",
        correspondent=c_auto,
        document_type=dt_auto,
        storage_path=sp_auto,
        checksum="aaa",
        mime_type="application/pdf",
        filename="invoice_a.pdf",
    )
    doc_a.tags.set([t_auto])

    doc_b = Document.objects.create(
        title="Invoice B",
        content="monthly invoice billing statement account balance due",
        correspondent=c_auto,
        document_type=dt_auto,
        storage_path=sp_auto,
        checksum="bbb",
        mime_type="application/pdf",
        filename="invoice_b.pdf",
    )
    doc_b.tags.set([t_auto])

    doc_c = Document.objects.create(
        title="Notes",
        content="meeting notes agenda discussion summary action items follow",
        checksum="ccc",
        mime_type="application/pdf",
        filename="notes_c.pdf",
    )

    return {"doc_a": doc_a, "doc_b": doc_b, "doc_c": doc_c}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestSingleQuerysetPass:
    def test_train_iterates_document_queryset_once(self, classifier, label_corpus):
        """
        train() must iterate the Document queryset exactly once.

        Before Phase 2 there were two iterations: one in the label extraction
        loop and a second inside content_generator() for CountVectorizer.
        After Phase 2 content is captured during the label loop; the second
        iteration is eliminated.
        """
        original_iter = QuerySet.__iter__
        doc_iter_count = 0

        def counting_iter(qs):
            nonlocal doc_iter_count
            if qs.model is Document:
                doc_iter_count += 1
            return original_iter(qs)

        with mock.patch.object(QuerySet, "__iter__", counting_iter):
            classifier.train()

        assert doc_iter_count == 1, (
            f"Expected 1 Document queryset iteration, got {doc_iter_count}. "
            "content_generator() may still be re-fetching from the DB."
        )

    def test_train_result_unchanged(self, classifier, label_corpus):
        """
        Collapsing to a single pass must not change what the classifier learns:
        a second train() with no changes still returns False.
        """
        assert classifier.train() is True
        assert classifier.train() is False
