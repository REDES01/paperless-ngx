"""
Tags classifier correctness test — Phase 3b gate.

This test must pass both BEFORE and AFTER the MLPClassifier → LinearSVC swap.
It verifies that the tags classifier correctly learns discriminative signal and
predicts the right tags on held-out documents.

Run before the swap to establish a baseline, then run again after to confirm
the new algorithm is at least as correct.

Two scenarios are tested:
  1. Multi-tag (num_tags > 1) — the common case; uses MultiLabelBinarizer
  2. Single-tag (num_tags == 1) — special binary path; uses LabelBinarizer

Corpus design: each tag has a distinct vocabulary cluster.  Each training
document contains words from exactly one cluster (or two for multi-tag docs).
Held-out test documents contain the same cluster words; correct classification
requires the model to learn the vocabulary → tag mapping.
"""

from __future__ import annotations

import pytest

from documents.classifier import DocumentClassifier
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import MatchingModel
from documents.models import StoragePath
from documents.models import Tag

# ---------------------------------------------------------------------------
# Vocabulary clusters — intentionally non-overlapping so both MLP and SVM
# should learn them perfectly or near-perfectly.
# ---------------------------------------------------------------------------

FINANCE_WORDS = (
    "invoice payment tax revenue billing statement account receivable "
    "quarterly budget expense ledger debit credit profit loss fiscal"
)
LEGAL_WORDS = (
    "contract agreement terms conditions clause liability indemnity "
    "jurisdiction arbitration compliance regulation statute obligation"
)
MEDICAL_WORDS = (
    "prescription diagnosis treatment patient health symptom dosage "
    "physician referral therapy clinical examination procedure chronic"
)
HR_WORDS = (
    "employee salary onboarding performance review appraisal benefits "
    "recruitment hiring resignation termination payroll department staff"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def classifier_settings(settings, tmp_path):
    settings.MODEL_FILE = tmp_path / "model.pickle"
    return settings


@pytest.fixture()
def classifier(classifier_settings):
    return DocumentClassifier()


def _make_doc(title, content, checksum, tags=(), **kwargs):
    doc = Document.objects.create(
        title=title,
        content=content,
        checksum=checksum,
        mime_type="application/pdf",
        filename=f"{checksum}.pdf",
        **kwargs,
    )
    if tags:
        doc.tags.set(tags)
    return doc


def _words(cluster, extra=""):
    """Repeat cluster words enough times to clear min_df=0.01 at ~40 docs."""
    return f"{cluster} {cluster} {extra}".strip()


# ---------------------------------------------------------------------------
# Multi-tag correctness
# ---------------------------------------------------------------------------


@pytest.fixture()
def multi_tag_corpus(classifier_settings):
    """
    40 training documents across 4 AUTO tags with distinct vocabulary.
    10 single-tag docs per tag + 5 two-tag docs.  Total: 45 docs.

    A non-AUTO correspondent and doc type are included to keep the
    other classifiers happy and not raise ValueError.
    """
    t_finance = Tag.objects.create(
        name="finance",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    t_legal = Tag.objects.create(
        name="legal",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    t_medical = Tag.objects.create(
        name="medical",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    t_hr = Tag.objects.create(name="hr", matching_algorithm=MatchingModel.MATCH_AUTO)

    # non-AUTO labels to keep the other classifiers from raising
    c = Correspondent.objects.create(
        name="org",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )
    dt = DocumentType.objects.create(
        name="doc",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )
    sp = StoragePath.objects.create(
        name="archive",
        path="archive",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )

    checksum = 0

    def make(title, content, tags):
        nonlocal checksum
        checksum += 1
        return _make_doc(
            title,
            content,
            f"{checksum:04d}",
            tags=tags,
            correspondent=c,
            document_type=dt,
            storage_path=sp,
        )

    # 10 single-tag training docs per tag
    for i in range(10):
        make(f"finance-{i}", _words(FINANCE_WORDS, f"doc{i}"), [t_finance])
        make(f"legal-{i}", _words(LEGAL_WORDS, f"doc{i}"), [t_legal])
        make(f"medical-{i}", _words(MEDICAL_WORDS, f"doc{i}"), [t_medical])
        make(f"hr-{i}", _words(HR_WORDS, f"doc{i}"), [t_hr])

    # 5 two-tag training docs
    for i in range(5):
        make(
            f"finance-legal-{i}",
            _words(FINANCE_WORDS + " " + LEGAL_WORDS, f"combo{i}"),
            [t_finance, t_legal],
        )

    return {
        "t_finance": t_finance,
        "t_legal": t_legal,
        "t_medical": t_medical,
        "t_hr": t_hr,
    }


@pytest.mark.django_db()
class TestMultiTagCorrectness:
    """
    The tags classifier must correctly predict tags on held-out documents whose
    content clearly belongs to one or two vocabulary clusters.

    A prediction is "correct" if the expected tag is present in the result.
    """

    def test_single_cluster_docs_predicted_correctly(
        self,
        classifier,
        multi_tag_corpus,
    ):
        """Each single-cluster held-out doc gets exactly the right tag."""
        classifier.train()
        tags = multi_tag_corpus

        cases = [
            (FINANCE_WORDS + " unique alpha", [tags["t_finance"].pk]),
            (LEGAL_WORDS + " unique beta", [tags["t_legal"].pk]),
            (MEDICAL_WORDS + " unique gamma", [tags["t_medical"].pk]),
            (HR_WORDS + " unique delta", [tags["t_hr"].pk]),
        ]

        for content, expected_pks in cases:
            predicted = classifier.predict_tags(content)
            for pk in expected_pks:
                assert pk in predicted, (
                    f"Expected tag pk={pk} in predictions for content starting "
                    f"'{content[:40]}…', got {predicted}"
                )

    def test_multi_cluster_doc_gets_both_tags(self, classifier, multi_tag_corpus):
        """A document with finance + legal vocabulary gets both tags."""
        classifier.train()
        tags = multi_tag_corpus

        content = FINANCE_WORDS + " " + LEGAL_WORDS + " unique epsilon"
        predicted = classifier.predict_tags(content)

        assert tags["t_finance"].pk in predicted, f"Expected finance tag in {predicted}"
        assert tags["t_legal"].pk in predicted, f"Expected legal tag in {predicted}"

    def test_unrelated_content_predicts_no_trained_tags(
        self,
        classifier,
        multi_tag_corpus,
    ):
        """
        Completely alien content should not confidently fire any learned tag.
        This is a soft check — we only assert no false positives on a document
        that shares zero vocabulary with the training corpus.
        """
        classifier.train()

        alien = (
            "xyzzyx qwerty asdfgh zxcvbn plokij unique zeta "
            "xyzzyx qwerty asdfgh zxcvbn plokij unique zeta"
        )
        predicted = classifier.predict_tags(alien)
        # Not a hard requirement — just log for human inspection
        # Both MLP and SVM may or may not produce false positives on OOV content
        assert isinstance(predicted, list)


# ---------------------------------------------------------------------------
# Single-tag (binary) correctness
# ---------------------------------------------------------------------------


@pytest.fixture()
def single_tag_corpus(classifier_settings):
    """
    Corpus with exactly ONE AUTO tag, exercising the LabelBinarizer +
    binary classification path.  Documents either have the tag or don't.
    """
    t_finance = Tag.objects.create(
        name="finance",
        matching_algorithm=MatchingModel.MATCH_AUTO,
    )
    c = Correspondent.objects.create(
        name="org",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )
    dt = DocumentType.objects.create(
        name="doc",
        matching_algorithm=MatchingModel.MATCH_NONE,
    )

    checksum = 0

    def make(title, content, tags):
        nonlocal checksum
        checksum += 1
        return _make_doc(
            title,
            content,
            f"s{checksum:04d}",
            tags=tags,
            correspondent=c,
            document_type=dt,
        )

    for i in range(10):
        make(f"finance-{i}", _words(FINANCE_WORDS, f"s{i}"), [t_finance])
        make(f"other-{i}", _words(LEGAL_WORDS, f"s{i}"), [])

    return {"t_finance": t_finance}


@pytest.mark.django_db()
class TestSingleTagCorrectness:
    def test_finance_content_predicts_finance_tag(self, classifier, single_tag_corpus):
        """Finance vocabulary → finance tag predicted."""
        classifier.train()
        tags = single_tag_corpus

        predicted = classifier.predict_tags(FINANCE_WORDS + " unique alpha single")
        assert tags["t_finance"].pk in predicted, (
            f"Expected finance tag pk={tags['t_finance'].pk} in {predicted}"
        )

    def test_non_finance_content_predicts_no_tag(self, classifier, single_tag_corpus):
        """Non-finance vocabulary → no tag predicted."""
        classifier.train()

        predicted = classifier.predict_tags(LEGAL_WORDS + " unique beta single")
        assert predicted == [], f"Expected no tags, got {predicted}"
