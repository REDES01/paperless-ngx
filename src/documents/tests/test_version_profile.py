"""
Profiling tests for the DocumentVersion model refactor.

Measures query count, wall time, and memory for the document list API
before and after replacing self-referential versioning with DocumentVersion.

Run with:
    cd src && uv run pytest documents/tests/test_version_profile.py \
        -m profiling --override-ini="addopts=" -s -v

Corpus parameters:
    DOCS_BASELINE   = 1000  (Scenario A: no versions)
    DOCS_VERSIONED  = 100   (Scenario B: 3 versions each)
    VERSIONS_PER_DOC = 3
    DETAIL_VERSIONS  = 5   (Scenario C: single detail)

NOTE: This file is intentionally NOT committed to git.
      It exists only to capture before/after profiling numbers.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from time import perf_counter

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db import reset_queries
from django.test.utils import override_settings
from faker import Faker
from rest_framework.test import APIClient

from documents.models import Document
from documents.models import DocumentVersion

# ---------------------------------------------------------------------------
# Try to import profiling helpers; define inline fallback if not available.
# ---------------------------------------------------------------------------

try:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
    from profiling import measure_memory
    from profiling import profile_block
    from profiling import profile_cpu  # noqa: F401
except ImportError:
    import tracemalloc
    from contextlib import contextmanager
    from typing import Any

    @contextmanager
    def profile_block(label: str = "block"):
        tracemalloc.start()
        snap_before = tracemalloc.take_snapshot()
        with override_settings(DEBUG=True):
            reset_queries()
            t0 = perf_counter()
            yield
            elapsed = perf_counter() - t0
            queries = list(connection.queries)
        snap_after = tracemalloc.take_snapshot()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stats = snap_after.compare_to(snap_before, "lineno")
        mem_diff = sum(s.size_diff for s in stats)
        query_time = sum(float(q["time"]) for q in queries)
        print(f"\n{'=' * 60}")  # noqa: T201
        print(f"  Profile: {label}")  # noqa: T201
        print(f"{'=' * 60}")  # noqa: T201
        print(f"  Wall time:    {elapsed:.4f}s")  # noqa: T201
        print(f"  Queries:      {len(queries)} ({query_time:.4f}s)")  # noqa: T201
        print(f"  Memory delta: {mem_diff / 1024:.1f} KiB")  # noqa: T201
        print(f"  Peak memory:  {peak / 1024:.1f} KiB")  # noqa: T201
        print("\n  Top 5 allocations:")  # noqa: T201
        for stat in stats[:5]:
            print(f"    {stat}")  # noqa: T201
        print(f"{'=' * 60}\n")  # noqa: T201

    def measure_memory(fn, *, label: str) -> tuple[Any, float, float]:
        tracemalloc.start()
        snap_before = tracemalloc.take_snapshot()
        result = fn()
        snap_after = tracemalloc.take_snapshot()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stats = snap_after.compare_to(snap_before, "lineno")
        delta_kib = sum(s.size_diff for s in stats) / 1024
        print(f"\n{'=' * 72}")  # noqa: T201
        print(f"  [memory] {label}")  # noqa: T201
        print(f"  memory delta: {delta_kib:+.1f} KiB")  # noqa: T201
        print(f"  peak traced:  {peak / 1024:.1f} KiB")  # noqa: T201
        print(f"{'=' * 72}")  # noqa: T201
        for stat in stats[:10]:
            if stat.size_diff != 0:
                print(  # noqa: T201
                    f"    {stat.size_diff / 1024:+8.1f} KiB  {stat.traceback.format()[0]}",
                )
        return result, peak / 1024, delta_kib


pytestmark = [pytest.mark.profiling, pytest.mark.django_db]

# ---------------------------------------------------------------------------
# Corpus parameters
# ---------------------------------------------------------------------------

DOCS_BASELINE = 1000
DOCS_VERSIONED = 100
VERSIONS_PER_DOC = 3
DETAIL_VERSIONS = 5
PAGE_SIZE = 25
SEED = 42


# ---------------------------------------------------------------------------
# Module-scoped DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module_db(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        yield


# ---------------------------------------------------------------------------
# Scenario A corpus: 1000 plain documents, no versions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus_baseline(module_db):
    fake = Faker()
    Faker.seed(SEED)

    owner = User.objects.create_superuser(
        username="vp_baseline_owner",
        password="admin",
    )

    raw = [
        Document(
            title=fake.sentence(nb_words=5).rstrip("."),
            content=fake.paragraph(nb_sentences=3),
            checksum=f"VPBASE{i:07d}",
            owner=owner,
        )
        for i in range(DOCS_BASELINE)
    ]
    t0 = time.perf_counter()
    Document.objects.bulk_create(raw)
    print(  # noqa: T201
        f"\n[corpus_baseline] bulk_create {DOCS_BASELINE} docs: "
        f"{time.perf_counter() - t0:.2f}s",
    )

    yield {"owner": owner}

    print("\n[corpus_baseline] teardown")  # noqa: T201
    Document.global_objects.filter(checksum__startswith="VPBASE").hard_delete()
    User.objects.filter(username="vp_baseline_owner").delete()


# ---------------------------------------------------------------------------
# Scenario B corpus: 100 documents each with VERSIONS_PER_DOC DocumentVersions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus_versioned(module_db):
    fake = Faker()
    Faker.seed(SEED + 1)

    owner = User.objects.create_superuser(
        username="vp_versioned_owner",
        password="admin",
    )

    root_docs = Document.objects.bulk_create(
        [
            Document(
                title=fake.sentence(nb_words=5).rstrip("."),
                content=fake.paragraph(nb_sentences=3),
                checksum=f"VPROOT{i:07d}",
                owner=owner,
            )
            for i in range(DOCS_VERSIONED)
        ],
    )

    version_rows = []
    for root in root_docs:
        for v in range(1, VERSIONS_PER_DOC + 1):
            version_rows.append(
                DocumentVersion(
                    document=root,
                    version_number=v,
                    checksum=f"VPVER{root.pk:06d}v{v}",
                    mime_type="application/pdf",
                ),
            )
    t0 = time.perf_counter()
    DocumentVersion.objects.bulk_create(version_rows)
    print(  # noqa: T201
        f"\n[corpus_versioned] bulk_create {len(version_rows)} document versions: "
        f"{time.perf_counter() - t0:.2f}s",
    )

    yield {"owner": owner, "root_docs": root_docs}

    print("\n[corpus_versioned] teardown")  # noqa: T201
    Document.global_objects.filter(checksum__startswith="VPROOT").hard_delete()
    User.objects.filter(username="vp_versioned_owner").delete()


# ---------------------------------------------------------------------------
# Scenario C corpus: single document with DETAIL_VERSIONS DocumentVersions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus_detail(module_db):
    fake = Faker()
    Faker.seed(SEED + 2)

    owner = User.objects.create_superuser(
        username="vp_detail_owner",
        password="admin",
    )

    root = Document.objects.create(
        title="Detail profile doc",
        content=fake.paragraph(nb_sentences=4),
        checksum="VPDETAILROOT",
        owner=owner,
    )

    for v in range(1, DETAIL_VERSIONS + 1):
        DocumentVersion.objects.create(
            document=root,
            version_number=v,
            checksum=f"VPDETAILVER{v}",
            mime_type="application/pdf",
        )

    yield {"owner": owner, "root_pk": root.pk}

    print("\n[corpus_detail] teardown")  # noqa: T201
    Document.global_objects.filter(checksum__startswith="VPDETAIL").hard_delete()
    User.objects.filter(username="vp_detail_owner").delete()


# ---------------------------------------------------------------------------
# Scenario A: Baseline — no versions, list endpoint
# ---------------------------------------------------------------------------


class TestScenarioA_BaselineNoVersions:
    """GET /api/documents/ with N plain documents, no version-children."""

    @pytest.fixture(autouse=True)
    def _setup(self, corpus_baseline):
        self.owner = corpus_baseline["owner"]
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _count_queries_for_list(self, page_size: int = PAGE_SIZE) -> list:
        with override_settings(DEBUG=True):
            reset_queries()
            resp = self.client.get(f"/api/documents/?page=1&page_size={page_size}")
            queries = list(connection.queries)
        assert resp.status_code == 200, resp.data
        return queries

    def test_a1_query_count_page25(self):
        queries = self._count_queries_for_list()

        print(f"\n  Query count (page_size={PAGE_SIZE}): {len(queries)}")  # noqa: T201
        for i, q in enumerate(queries):
            print(f"  [{i}] {q['sql'][:120]}  --  {q['time']}s")  # noqa: T201

        # After refactor: root_document_id must not appear in any query
        main_queries = [q for q in queries if "documents_document" in q["sql"]]
        subquery_refs = [q for q in main_queries if "root_document_id" in q["sql"]]
        print(  # noqa: T201
            f"  Queries referencing root_document_id: {len(subquery_refs)}"
            " (must be 0 after refactor)",
        )
        assert len(subquery_refs) == 0, (
            "root_document_id must not appear in queries after refactor"
        )

        # First request in session adds 2 content_type warmup queries; steady-state is 8.
        assert len(queries) <= 12

    def test_a2_wall_time_n100(self):
        with profile_block(
            f"Scenario A — GET /api/documents/ page_size={PAGE_SIZE}, "
            f"N={DOCS_BASELINE} baseline docs",
        ):
            resp = self.client.get(f"/api/documents/?page=1&page_size={PAGE_SIZE}")
        assert resp.status_code == 200

    def test_a3_memory_list_serialization(self):
        _, peak_kib, delta_kib = measure_memory(
            lambda: self.client.get(f"/api/documents/?page=1&page_size={PAGE_SIZE}"),
            label=f"Scenario A — serialize page_size={PAGE_SIZE} plain docs",
        )
        print(  # noqa: T201
            f"\n  Peak KiB: {peak_kib:.1f}  Delta KiB: {delta_kib:.1f}\n"
            "  (baseline after refactor)",
        )

    def test_a4_wall_time_comparison_sizes(self):
        """Compare wall time at page_size=10, 25, 100 to quantify subquery slope."""
        results = {}
        for page_size in [10, 25, 100]:
            t0 = perf_counter()
            resp = self.client.get(f"/api/documents/?page=1&page_size={page_size}")
            elapsed_ms = (perf_counter() - t0) * 1000
            assert resp.status_code == 200
            results[page_size] = elapsed_ms

        print("\n  Wall time by page_size:")  # noqa: T201
        for ps, ms in results.items():
            print(f"    page_size={ps:4d}: {ms:.1f} ms")  # noqa: T201

        ratio = results[100] / max(results[25], 0.1)
        print(  # noqa: T201
            f"  time(100)/time(25) ratio: {ratio:.2f}"
            " (before refactor often > 4.0; target < 3.0 after)",
        )


# ---------------------------------------------------------------------------
# Scenario B: Versioned documents — list endpoint
# ---------------------------------------------------------------------------


class TestScenarioB_VersionedDocsList:
    """GET /api/documents/ with 100 docs each having 3 DocumentVersions."""

    @pytest.fixture(autouse=True)
    def _setup(self, corpus_versioned):
        self.owner = corpus_versioned["owner"]
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_b1_query_count_versioned(self):
        with override_settings(DEBUG=True):
            reset_queries()
            resp = self.client.get(f"/api/documents/?page=1&page_size={PAGE_SIZE}")
            queries = list(connection.queries)

        assert resp.status_code == 200
        print(f"\n  Query count (versioned corpus): {len(queries)}")  # noqa: T201
        for i, q in enumerate(queries):
            print(f"  [{i}] {q['sql'][:120]}  --  {q['time']}s")  # noqa: T201

        assert len(queries) <= 8

    def test_b2_memory_versioned_list(self):
        _, peak_kib, delta_kib = measure_memory(
            lambda: self.client.get(f"/api/documents/?page=1&page_size={PAGE_SIZE}"),
            label=(
                f"Scenario B — {DOCS_VERSIONED} docs x {VERSIONS_PER_DOC} versions, "
                f"page_size={PAGE_SIZE}"
            ),
        )
        print(  # noqa: T201
            f"\n  Peak KiB: {peak_kib:.1f}  Delta KiB: {delta_kib:.1f}\n"
            "  Target after refactor: delta < 60% of pre-refactor value",
        )

    def test_b3_wall_time_versioned(self):
        with profile_block(
            f"Scenario B — versioned list page_size={PAGE_SIZE}, "
            f"{DOCS_VERSIONED} docs x {VERSIONS_PER_DOC} versions",
        ):
            resp = self.client.get(f"/api/documents/?page=1&page_size={PAGE_SIZE}")
        assert resp.status_code == 200

    def test_b4_no_root_document_id_in_queries(self):
        """Structural assertion: root_document_id must not appear in queries after refactor."""
        with override_settings(DEBUG=True):
            reset_queries()
            self.client.get(f"/api/documents/?page=1&page_size={PAGE_SIZE}")
            queries = list(connection.queries)

        doc_queries = [
            q
            for q in queries
            if "documents_document" in q["sql"] and "SELECT" in q["sql"].upper()
        ]
        subquery_present = any("root_document_id" in q["sql"] for q in doc_queries)
        print(  # noqa: T201
            f"\n  root_document_id in SELECT queries: {subquery_present}\n"
            "  AFTER refactor: must be False.",
        )
        assert not subquery_present, "root_document_id must not appear after refactor"


# ---------------------------------------------------------------------------
# Scenario C: Single document detail, 5 versions
# ---------------------------------------------------------------------------


class TestScenarioC_DetailWithVersions:
    """GET /api/documents/{id}/ for a document with 5 DocumentVersions."""

    @pytest.fixture(autouse=True)
    def _setup(self, corpus_detail):
        self.owner = corpus_detail["owner"]
        self.root_pk = corpus_detail["root_pk"]
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_c1_query_count_detail(self):
        with override_settings(DEBUG=True):
            reset_queries()
            resp = self.client.get(f"/api/documents/{self.root_pk}/")
            queries = list(connection.queries)

        assert resp.status_code == 200
        print(f"\n  Detail query count: {len(queries)}")  # noqa: T201
        for i, q in enumerate(queries):
            print(f"  [{i}] {q['sql'][:120]}  --  {q['time']}s")  # noqa: T201

        assert len(queries) <= 10

    def test_c2_wall_time_detail(self):
        with profile_block(
            f"Scenario C — GET /api/documents/{self.root_pk}/ "
            f"({DETAIL_VERSIONS} versions)",
        ):
            resp = self.client.get(f"/api/documents/{self.root_pk}/")
        assert resp.status_code == 200

    def test_c3_versions_field_in_response(self):
        resp = self.client.get(f"/api/documents/{self.root_pk}/")
        assert resp.status_code == 200
        versions = resp.data.get("versions", [])
        print(  # noqa: T201
            f"\n  versions count in response: {len(versions)}\n"
            f"  After refactor: expect {DETAIL_VERSIONS} DocumentVersion records",
        )
        assert len(versions) == DETAIL_VERSIONS


# ---------------------------------------------------------------------------
# Scenario D: Version creation (consumer path, simulated)
# ---------------------------------------------------------------------------


class TestScenarioD_VersionCreation:
    """
    Query cost of creating a new document version.

    BEFORE refactor:
        SELECT FOR UPDATE on Document row (1 query)
        Aggregate max(version_index) on Document (1 query)
        INSERT new Document row (1 query)
        = 3 + savepoint overhead

    AFTER refactor:
        SELECT count on DocumentVersion rows (1 query)
        INSERT new DocumentVersion row (1 query)
        = 2 + savepoint overhead (smaller INSERT + narrower table)
    """

    @pytest.fixture(autouse=True)
    def _setup(self, db):
        fake = Faker()
        owner = User.objects.create_superuser(
            username="vp_create_owner",
            password="admin",
        )
        self.root_doc = Document.objects.create(
            title="Version creation test doc",
            content=fake.paragraph(),
            checksum="VPCREATEROOT",
            owner=owner,
        )
        self.owner = owner
        yield
        Document.global_objects.filter(checksum__startswith="VPCREATE").hard_delete()
        User.objects.filter(username="vp_create_owner").delete()

    def test_d1_version_creation_query_count(self):
        from django.db import transaction

        with override_settings(DEBUG=True):
            reset_queries()
            with transaction.atomic():
                next_number = (
                    DocumentVersion.objects.filter(
                        document=self.root_doc,
                    ).count()
                    + 1
                )
                DocumentVersion(
                    document=self.root_doc,
                    version_number=next_number,
                    checksum=f"VPCREATEVER{next_number}",
                    mime_type="application/pdf",
                ).save()

            queries = list(connection.queries)

        print(f"\n  Version creation query count: {len(queries)}")  # noqa: T201
        for i, q in enumerate(queries):
            print(f"  [{i}] {q['sql'][:120]}  --  {q['time']}s")  # noqa: T201

        assert len(queries) <= 8

    def test_d2_version_creation_wall_time(self):
        from django.db import transaction

        times = []
        for i in range(10):
            t0 = perf_counter()
            with transaction.atomic():
                next_number = (
                    DocumentVersion.objects.filter(
                        document=self.root_doc,
                    ).count()
                    + 1
                )
                DocumentVersion(
                    document=self.root_doc,
                    version_number=next_number,
                    checksum=f"VPCREATEBENCH{i}",
                    mime_type="application/pdf",
                ).save()
            times.append((perf_counter() - t0) * 1000)

        avg_ms = sum(times) / len(times)
        print(  # noqa: T201
            f"\n  Version creation: avg {avg_ms:.1f} ms over 10 runs\n"
            f"  Min: {min(times):.1f} ms  Max: {max(times):.1f} ms\n"
            "  After refactor: INSERT on DocumentVersion (narrower) should be faster.",
        )
        assert avg_ms < 500


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_z_print_summary(self, db):
        print("""
========================================================================
  DocumentVersion Refactor — Baseline Numbers
========================================================================

  PRE-REFACTOR (self-referential Document.root_document):
  -------------------------------------------------------
  SCENARIO A: List endpoint, no versions (N=1000 docs)
  Metric                     BEFORE
  Query count (page=25)      _____
  root_document_id refs      _____
  Wall time (page=25, ms)    _____
  Memory delta (KiB)         _____
  time(100)/time(25) ratio   _____

  SCENARIO B: List, 100 docs x 3 version-children (old Document rows)
  Metric                     BEFORE
  Query count (page=25)      _____
  Memory delta (KiB)         _____
  Peak memory (KiB)          _____
  Wall time (page=25, ms)    _____

  SCENARIO C: Detail, 1 doc with 5 version-children
  Metric                     BEFORE
  Query count                _____
  Wall time (ms)             _____
  versions in response       _____

  SCENARIO D: Version creation (10 runs avg, old Document INSERT)
  Metric                     BEFORE
  Query count                _____
  Avg wall time (ms)         _____

  POST-REFACTOR (DocumentVersion model):
  -------------------------------------------------------
  Fill in numbers from current test output above.

  SCENARIO A: List endpoint, no versions (N=1000 docs)
  Metric                     AFTER      Target
  Query count (page=25)      _____      <= 8
  root_document_id refs       0         == 0
  Wall time (page=25, ms)    _____      < 70% of BEFORE
  Memory delta (KiB)         _____      < 80% of BEFORE
  time(100)/time(25) ratio   _____      < 3.0

  SCENARIO B: List, 100 docs x 3 DocumentVersions
  Metric                     AFTER      Target
  Query count (page=25)      _____      <= 8
  Memory delta (KiB)         _____      < 60% of BEFORE
  Peak memory (KiB)          _____      < 70% of BEFORE
  Wall time (page=25, ms)    _____      < 70% of BEFORE

  SCENARIO C: Detail, 1 doc with 5 DocumentVersions
  Metric                     AFTER      Target
  Query count                _____      <= 10
  Wall time (ms)             _____      < 80% of BEFORE
  versions in response       _____      == DETAIL_VERSIONS

  SCENARIO D: Version creation (10 runs avg, DocumentVersion INSERT)
  Metric                     AFTER      Target
  Query count                _____      <= 8
  Avg wall time (ms)         _____      lower (narrower INSERT)

========================================================================
""")  # noqa: T201
