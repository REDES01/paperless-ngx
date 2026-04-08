"""
Phase 1 — fast-skip optimisation in DocumentClassifier.train()

The goal: when nothing has changed since the last training run, train() should
return False after at most 5 DB queries (1x MAX(modified) + 4x MATCH_AUTO pk
lists), not after a full per-document label scan.

Correctness invariant: the skip must NOT fire when the set of AUTO-matching
labels has changed, even if no Document.modified timestamp has advanced (e.g.
a Tag's matching_algorithm was flipped to MATCH_AUTO after the last train).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from documents.classifier import DocumentClassifier
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import MatchingModel
from documents.models import StoragePath
from documents.models import Tag

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def classifier_settings(settings, tmp_path: Path):
    """Point MODEL_FILE at a temp directory so tests are hermetic."""
    settings.MODEL_FILE = tmp_path / "model.pickle"
    return settings


@pytest.fixture()
def classifier(classifier_settings):
    """Fresh DocumentClassifier instance with test settings active."""
    return DocumentClassifier()


@pytest.fixture()
def label_corpus(classifier_settings):
    """
    Minimal label + document corpus that produces a trainable classifier.

    Creates
    -------
    Correspondents
        c_auto   — MATCH_AUTO, assigned to two docs
        c_none   — MATCH_NONE (control)
    DocumentTypes
        dt_auto  — MATCH_AUTO, assigned to two docs
        dt_none  — MATCH_NONE (control)
    Tags
        t_auto   — MATCH_AUTO, applied to two docs
        t_none   — MATCH_NONE (control, applied to one doc but never learned)
    StoragePaths
        sp_auto  — MATCH_AUTO, assigned to two docs
        sp_none  — MATCH_NONE (control)

    Documents
        doc_a, doc_b — assigned AUTO labels above
        doc_c        — control doc (MATCH_NONE labels only)

    The fixture returns a dict with all created objects for direct mutation in
    individual tests.
    """
    c_auto = Correspondent.objects.create(
        name="Auto Corp",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    c_none = Correspondent.objects.create(
        name="Manual Corp",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )

    dt_auto = DocumentType.objects.create(
        name="Invoice",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    dt_none = DocumentType.objects.create(
        name="Other",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )

    t_auto = Tag.objects.create(
        name="finance",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    t_none = Tag.objects.create(
        name="misc",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )

    sp_auto = StoragePath.objects.create(
        name="Finance Path",
        path="finance/{correspondent}",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    sp_none = StoragePath.objects.create(
        name="Other Path",
        path="other/{correspondent}",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )

    doc_a = Document.objects.create(
        title="Invoice from Auto Corp Jan",
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
        title="Invoice from Auto Corp Feb",
        content="monthly invoice billing statement account balance due",
        correspondent=c_auto,
        document_type=dt_auto,
        storage_path=sp_auto,
        checksum="bbb",
        mime_type="application/pdf",
        filename="invoice_b.pdf",
    )
    doc_b.tags.set([t_auto])

    # Control document — no AUTO labels, but has enough content to vectorize
    doc_c = Document.objects.create(
        title="Miscellaneous Notes",
        content="meeting notes agenda discussion summary action items follow",
        correspondent=c_none,
        document_type=dt_none,
        checksum="ccc",
        mime_type="application/pdf",
        filename="notes_c.pdf",
    )
    doc_c.tags.set([t_none])

    return {
        "c_auto": c_auto,
        "c_none": c_none,
        "dt_auto": dt_auto,
        "dt_none": dt_none,
        "t_auto": t_auto,
        "t_none": t_none,
        "sp_auto": sp_auto,
        "sp_none": sp_none,
        "doc_a": doc_a,
        "doc_b": doc_b,
        "doc_c": doc_c,
    }


# ---------------------------------------------------------------------------
# Happy-path skip tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestFastSkipFires:
    """The no-op path: nothing changed, so the second train() is skipped."""

    def test_first_train_returns_true(self, classifier, label_corpus):
        """First train on a fresh classifier must return True (did work)."""
        assert classifier.train() is True

    def test_second_train_returns_false(self, classifier, label_corpus):
        """Second train with no changes must return False (skipped)."""
        classifier.train()
        assert classifier.train() is False

    def test_fast_skip_runs_minimal_queries(self, classifier, label_corpus):
        """
        The no-op path must use at most 5 DB queries:
          1x Document.objects.aggregate(Max('modified'))
          4x MATCH_AUTO pk lists  (Correspondent / DocumentType / Tag / StoragePath)

        The current implementation (before Phase 1) iterates every document
        to build the label hash BEFORE it can decide to skip, which is O(N).
        This test verifies the fast path is in place.
        """
        classifier.train()
        with CaptureQueriesContext(connection) as ctx:
            result = classifier.train()
        assert result is False
        assert len(ctx.captured_queries) <= 5, (
            f"Fast skip used {len(ctx.captured_queries)} queries; expected ≤5.\n"
            + "\n".join(q["sql"] for q in ctx.captured_queries)
        )

    def test_fast_skip_refreshes_cache_keys(self, classifier, label_corpus):
        """
        Even on a skip, the cache keys must be refreshed so that the task
        scheduler can detect the classifier is still current.
        """
        from django.core.cache import cache

        from documents.caching import CLASSIFIER_HASH_KEY
        from documents.caching import CLASSIFIER_MODIFIED_KEY
        from documents.caching import CLASSIFIER_VERSION_KEY

        classifier.train()
        # Evict the keys to prove skip re-populates them
        cache.delete(CLASSIFIER_MODIFIED_KEY)
        cache.delete(CLASSIFIER_HASH_KEY)
        cache.delete(CLASSIFIER_VERSION_KEY)

        result = classifier.train()

        assert result is False
        assert cache.get(CLASSIFIER_MODIFIED_KEY) is not None
        assert cache.get(CLASSIFIER_HASH_KEY) is not None
        assert cache.get(CLASSIFIER_VERSION_KEY) is not None


# ---------------------------------------------------------------------------
# Correctness tests — skip must NOT fire when the world has changed
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestFastSkipDoesNotFire:
    """The skip guard must yield to a full retrain whenever labels change."""

    def test_document_content_modification_triggers_retrain(
        self,
        classifier,
        label_corpus,
    ):
        """Updating a document's content updates modified → retrain required."""
        classifier.train()
        doc_a = label_corpus["doc_a"]
        doc_a.content = "completely different words here now nothing same"
        doc_a.save()
        assert classifier.train() is True

    def test_document_label_reassignment_triggers_retrain(
        self,
        classifier,
        label_corpus,
    ):
        """
        Reassigning a document to a different AUTO correspondent (touching
        doc.modified) must trigger a retrain.
        """
        c_auto2 = Correspondent.objects.create(
            name="Second Auto Corp",
            matching_algorithm=MatchingModel.MATCH_AUTO,
        )
        classifier.train()
        doc_a = label_corpus["doc_a"]
        doc_a.correspondent = c_auto2
        doc_a.save()
        assert classifier.train() is True

    def test_matching_algorithm_change_on_assigned_tag_triggers_retrain(
        self,
        classifier,
        label_corpus,
    ):
        """
        Flipping a tag's matching_algorithm to MATCH_AUTO after it is already
        assigned to documents must trigger a retrain — even though no
        Document.modified timestamp advances.

        This is the key correctness case for the auto-label-set digest:
        the tag is already on doc_a and doc_b, so once it becomes MATCH_AUTO
        the classifier needs to learn it.
        """
        # t_none is applied to doc_c (a control doc) via the fixture.
        # We flip it to MATCH_AUTO; the set of learnable AUTO tags grows.
        classifier.train()
        t_none = label_corpus["t_none"]
        t_none.matching_algorithm = MatchingModel.MATCH_AUTO
        t_none.save(update_fields=["matching_algorithm"])
        # Document.modified is NOT touched — this test specifically verifies
        # that the auto-label-set digest catches the change.
        assert classifier.train() is True

    def test_new_auto_correspondent_triggers_retrain(self, classifier, label_corpus):
        """
        Adding a brand-new MATCH_AUTO correspondent (unassigned to any doc)
        must trigger a retrain: the auto-label-set has grown.
        """
        classifier.train()
        Correspondent.objects.create(
            name="New Auto Corp",
            matching_algorithm=MatchingModel.MATCH_AUTO,
        )
        assert classifier.train() is True

    def test_removing_auto_label_triggers_retrain(self, classifier, label_corpus):
        """
        Deleting a MATCH_AUTO correspondent shrinks the auto-label-set and
        must trigger a retrain.
        """
        classifier.train()
        label_corpus["c_auto"].delete()
        assert classifier.train() is True

    def test_fresh_classifier_always_trains(self, classifier, label_corpus):
        """
        A classifier that has never been trained (last_doc_change_time is None)
        must always perform a full train, regardless of corpus state.
        """
        assert classifier.last_doc_change_time is None
        assert classifier.train() is True

    def test_no_documents_raises_value_error(self, classifier, classifier_settings):
        """train() with an empty database must raise ValueError."""
        with pytest.raises(ValueError, match="No training data"):
            classifier.train()
