from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from auditlog.models import LogEntry  # type: ignore[import-untyped]
from django.contrib.auth.models import Permission
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from documents.data_models import DocumentSource
from documents.filters import EffectiveContentFilter
from documents.filters import TitleContentFilter
from documents.models import Document
from documents.models import DocumentVersion
from documents.tests.utils import DirectoriesMixin

if TYPE_CHECKING:
    from pathlib import Path


class TestDocumentVersioningApi(DirectoriesMixin, APITestCase):
    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=self.user)

    def _make_pdf_upload(self, name: str = "version.pdf") -> SimpleUploadedFile:
        return SimpleUploadedFile(
            name,
            b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
            content_type="application/pdf",
        )

    def _write_file(self, path: Path, content: bytes = b"data") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _create_doc(self, *, title: str, checksum: str) -> Document:
        doc = Document.objects.create(
            title=title,
            checksum=checksum,
            mime_type="application/pdf",
        )
        self._write_file(doc.source_path, b"pdf")
        self._write_file(doc.thumbnail_path, b"thumb")
        return doc

    def _create_version(
        self,
        doc: Document,
        *,
        version_number: int,
        checksum: str,
    ) -> DocumentVersion:
        v = DocumentVersion.objects.create(
            document=doc,
            version_number=version_number,
            checksum=checksum,
            mime_type="application/pdf",
            filename=f"version_{doc.pk}_v{version_number}.pdf",
        )
        self._write_file(v.source_path, b"pdf")
        self._write_file(v.thumbnail_path, b"thumb")
        return v

    def test_delete_version_disallows_deleting_last_version(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        version = self._create_version(root, version_number=1, checksum="v1")

        with mock.patch("documents.search.get_backend"):
            resp = self.client.delete(
                f"/api/documents/{root.id}/versions/{version.pk}/",
            )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only remaining version", resp.content.decode())
        self.assertTrue(DocumentVersion.objects.filter(pk=version.pk).exists())

    def test_delete_version_deletes_version_and_returns_current_version(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            content="root-content",
        )
        v1 = self._create_version(root, version_number=1, checksum="v1")
        v1.content = "v1-content"
        v1.save(update_fields=["content"])
        v2 = self._create_version(root, version_number=2, checksum="v2")
        v2.content = "v2-content"
        v2.save(update_fields=["content"])

        with mock.patch("documents.search.get_backend"):
            resp = self.client.delete(
                f"/api/documents/{root.id}/versions/{v2.pk}/",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(DocumentVersion.objects.filter(pk=v2.pk).exists())
        self.assertEqual(resp.data["current_version_id"], v1.pk)
        root.refresh_from_db()
        self.assertEqual(root.content, "v1-content")

        with mock.patch("documents.search.get_backend"):
            resp = self.client.delete(
                f"/api/documents/{root.id}/versions/{v1.pk}/",
            )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only remaining version", resp.content.decode())

    def test_delete_version_writes_audit_log_entry(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        self._create_version(root, version_number=1, checksum="v1")
        v2 = self._create_version(root, version_number=2, checksum="v2")
        version_pk = v2.pk

        with mock.patch("documents.search.get_backend"):
            resp = self.client.delete(
                f"/api/documents/{root.id}/versions/{version_pk}/",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Audit log entry is created against the root document.
        entry = (
            LogEntry.objects.filter(
                content_type=ContentType.objects.get_for_model(Document),
                object_id=root.id,
            )
            .order_by("-timestamp")
            .first()
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertIsNotNone(entry.actor)
        assert entry.actor is not None
        self.assertEqual(entry.actor.id, self.user.id)
        self.assertEqual(entry.action, LogEntry.Action.UPDATE)
        self.assertEqual(
            entry.changes,
            {"Version Deleted": ["None", version_pk]},
        )
        additional_data = entry.additional_data or {}
        self.assertEqual(additional_data.get("version_id"), version_pk)

    def test_delete_version_returns_404_when_version_not_related(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        other_root = Document.objects.create(
            title="other",
            checksum="other",
            mime_type="application/pdf",
        )
        other_v1 = self._create_version(
            other_root,
            version_number=1,
            checksum="other-v1",
        )
        self._create_version(other_root, version_number=2, checksum="other-v2")

        with mock.patch("documents.search.get_backend"):
            resp = self.client.delete(
                f"/api/documents/{root.id}/versions/{other_v1.pk}/",
            )

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_version_returns_404_when_root_missing(self) -> None:
        resp = self.client.delete("/api/documents/9999/versions/123/")

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_version_reindexes_root_document(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        self._create_version(root, version_number=1, checksum="v1")
        v2 = self._create_version(root, version_number=2, checksum="v2")

        with mock.patch("documents.search.get_backend") as mock_get_backend:
            mock_backend = mock.MagicMock()
            mock_get_backend.return_value = mock_backend
            resp = self.client.delete(
                f"/api/documents/{root.id}/versions/{v2.pk}/",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        mock_backend.add_or_update.assert_called_once()
        self.assertEqual(mock_backend.add_or_update.call_args[0][0].id, root.id)

    def test_delete_version_returns_403_without_permission(self) -> None:
        owner = User.objects.create_user(username="owner")
        other = User.objects.create_user(username="other")
        other.user_permissions.add(
            Permission.objects.get(codename="delete_document"),
        )
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            owner=owner,
        )
        version = self._create_version(root, version_number=1, checksum="v1")
        self.client.force_authenticate(user=other)

        resp = self.client.delete(
            f"/api/documents/{root.id}/versions/{version.pk}/",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_version_returns_404_when_version_missing(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )

        resp = self.client.delete(f"/api/documents/{root.id}/versions/9999/")

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_version_label_updates_and_trims(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        # version_number=1 is considered the root version; use 2 for a non-root.
        self._create_version(root, version_number=1, checksum="v1")
        version = self._create_version(root, version_number=2, checksum="v2")
        version.version_label = "old"
        version.save(update_fields=["version_label"])

        resp = self.client.patch(
            f"/api/documents/{root.id}/versions/{version.pk}/",
            {"version_label": "  Label 1  "},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        version.refresh_from_db()
        self.assertEqual(version.version_label, "Label 1")
        self.assertEqual(resp.data["version_label"], "Label 1")
        self.assertEqual(resp.data["id"], version.pk)
        self.assertFalse(resp.data["is_root"])

    def test_update_version_label_clears_on_blank(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        version = self._create_version(root, version_number=1, checksum="v1")
        version.version_label = "Root Label"
        version.save(update_fields=["version_label"])

        resp = self.client.patch(
            f"/api/documents/{root.id}/versions/{version.pk}/",
            {"version_label": "   "},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        version.refresh_from_db()
        self.assertIsNone(version.version_label)
        self.assertIsNone(resp.data["version_label"])
        self.assertTrue(resp.data["is_root"])

    def test_update_version_label_returns_403_without_permission(self) -> None:
        owner = User.objects.create_user(username="owner")
        other = User.objects.create_user(username="other")
        other.user_permissions.add(
            Permission.objects.get(codename="change_document"),
        )
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            owner=owner,
        )
        version = self._create_version(root, version_number=1, checksum="v1")
        self.client.force_authenticate(user=other)

        resp = self.client.patch(
            f"/api/documents/{root.id}/versions/{version.pk}/",
            {"version_label": "Blocked"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_version_label_returns_404_for_unrelated_version(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        other_root = Document.objects.create(
            title="other",
            checksum="other",
            mime_type="application/pdf",
        )
        other_version = self._create_version(
            other_root,
            version_number=1,
            checksum="other-v1",
        )

        resp = self.client.patch(
            f"/api/documents/{root.id}/versions/{other_version.pk}/",
            {"version_label": "Nope"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_download_version_param_errors(self) -> None:
        root = self._create_doc(title="root", checksum="root")

        resp = self.client.get(
            f"/api/documents/{root.id}/download/?version=not-a-number",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        resp = self.client.get(f"/api/documents/{root.id}/download/?version=9999")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        other_root = self._create_doc(title="other", checksum="other")
        other_version = self._create_version(
            other_root,
            version_number=1,
            checksum="other-v1",
        )
        resp = self.client.get(
            f"/api/documents/{root.id}/download/?version={other_version.pk}",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_download_preview_thumb_with_version_param(self) -> None:
        root = self._create_doc(title="root", checksum="root")
        version = self._create_version(root, version_number=1, checksum="v1")
        self._write_file(version.source_path, b"version")
        self._write_file(version.thumbnail_path, b"thumb")

        resp = self.client.get(
            f"/api/documents/{root.id}/download/?version={version.pk}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.content, b"version")

        resp = self.client.get(
            f"/api/documents/{root.id}/preview/?version={version.pk}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.content, b"version")

        resp = self.client.get(
            f"/api/documents/{root.id}/thumb/?version={version.pk}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.content, b"thumb")

    def test_metadata_version_param_uses_version(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        version = self._create_version(root, version_number=1, checksum="v1")

        with mock.patch("documents.views.DocumentViewSet.get_metadata") as metadata:
            metadata.return_value = []
            resp = self.client.get(
                f"/api/documents/{root.id}/metadata/?version={version.pk}",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(metadata.called)

    def test_metadata_version_param_errors(self) -> None:
        root = self._create_doc(title="root", checksum="root")

        resp = self.client.get(
            f"/api/documents/{root.id}/metadata/?version=not-a-number",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        resp = self.client.get(f"/api/documents/{root.id}/metadata/?version=9999")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        other_root = self._create_doc(title="other", checksum="other")
        other_version = self._create_version(
            other_root,
            version_number=1,
            checksum="other-v1",
        )
        resp = self.client.get(
            f"/api/documents/{root.id}/metadata/?version={other_version.pk}",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_metadata_returns_403_when_user_lacks_permission(self) -> None:
        owner = User.objects.create_user(username="owner")
        other = User.objects.create_user(username="other")
        other.user_permissions.add(
            Permission.objects.get(codename="view_document"),
        )
        doc = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            owner=owner,
        )
        self.client.force_authenticate(user=other)

        resp = self.client.get(f"/api/documents/{doc.id}/metadata/")

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_version_enqueues_consume_with_overrides(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        upload = self._make_pdf_upload()

        async_task = mock.Mock()
        async_task.id = "task-123"

        with mock.patch("documents.views.consume_file") as consume_mock:
            consume_mock.delay.return_value = async_task
            resp = self.client.post(
                f"/api/documents/{root.id}/update_version/",
                {"document": upload, "version_label": "  New Version  "},
                format="multipart",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, "task-123")
        consume_mock.delay.assert_called_once()
        input_doc, overrides = consume_mock.delay.call_args[0]
        self.assertEqual(input_doc.root_document_id, root.id)
        self.assertEqual(input_doc.source, DocumentSource.ApiUpload)
        self.assertEqual(overrides.version_label, "New Version")
        self.assertEqual(overrides.actor_id, self.user.id)

    def test_update_version_returns_500_on_consume_failure(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )
        upload = self._make_pdf_upload()

        with mock.patch("documents.views.consume_file") as consume_mock:
            consume_mock.delay.side_effect = Exception("boom")
            resp = self.client.post(
                f"/api/documents/{root.id}/update_version/",
                {"document": upload},
                format="multipart",
            )

        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_update_version_returns_403_without_permission(self) -> None:
        owner = User.objects.create_user(username="owner")
        other = User.objects.create_user(username="other")
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            owner=owner,
        )
        self.client.force_authenticate(user=other)

        resp = self.client.post(
            f"/api/documents/{root.id}/update_version/",
            {"document": self._make_pdf_upload()},
            format="multipart",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_version_returns_404_for_missing_document(self) -> None:
        resp = self.client.post(
            "/api/documents/9999/update_version/",
            {"document": self._make_pdf_upload()},
            format="multipart",
        )

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_version_requires_document(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
        )

        resp = self.client.post(
            f"/api/documents/{root.id}/update_version/",
            {"version_label": "label"},
            format="multipart",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_content_updates_latest_version_content(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            content="root-content",
        )
        v1 = self._create_version(root, version_number=1, checksum="v1")
        v1.content = "v1-content"
        v1.save(update_fields=["content"])
        v2 = self._create_version(root, version_number=2, checksum="v2")
        v2.content = "v2-content"
        v2.save(update_fields=["content"])

        with mock.patch("documents.search.get_backend"):
            resp = self.client.patch(
                f"/api/documents/{root.id}/",
                {"content": "edited-content"},
                format="json",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["content"], "edited-content")
        v1.refresh_from_db()
        v2.refresh_from_db()
        root.refresh_from_db()
        # The latest version (v2) and the Document cache are both updated.
        self.assertEqual(v2.content, "edited-content")
        self.assertEqual(root.content, "edited-content")
        self.assertEqual(v1.content, "v1-content")

    def test_patch_content_updates_selected_version_content(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            content="root-content",
        )
        v1 = self._create_version(root, version_number=1, checksum="v1")
        v1.content = "v1-content"
        v1.save(update_fields=["content"])
        v2 = self._create_version(root, version_number=2, checksum="v2")
        v2.content = "v2-content"
        v2.save(update_fields=["content"])

        with mock.patch("documents.search.get_backend"):
            resp = self.client.patch(
                f"/api/documents/{root.id}/?version={v1.pk}",
                {"content": "edited-v1"},
                format="json",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["content"], "edited-v1")
        v1.refresh_from_db()
        v2.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(v1.content, "edited-v1")
        self.assertEqual(v2.content, "v2-content")
        self.assertEqual(root.content, "root-content")

    def test_retrieve_returns_document_content(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            content="latest-content",
        )

        resp = self.client.get(f"/api/documents/{root.id}/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["content"], "latest-content")

    def test_retrieve_with_version_param_returns_selected_version_content(self) -> None:
        root = Document.objects.create(
            title="root",
            checksum="root",
            mime_type="application/pdf",
            content="root-content",
        )
        v1 = self._create_version(root, version_number=1, checksum="v1")
        v1.content = "v1-content"
        v1.save(update_fields=["content"])

        resp = self.client.get(f"/api/documents/{root.id}/?version={v1.pk}")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["content"], "v1-content")


class TestVersionAwareFilters:
    def test_title_content_filter_queries_content_directly(self) -> None:
        queryset = mock.Mock()

        TitleContentFilter().filter(queryset, " latest ")

        assert queryset.filter.call_count == 1

    def test_effective_content_filter_queries_content_directly(self) -> None:
        queryset = mock.Mock()

        EffectiveContentFilter(lookup_expr="icontains").filter(queryset, " latest ")

        assert queryset.filter.call_count == 1
        kwargs = queryset.filter.call_args_list[0].kwargs
        assert kwargs == {"content__icontains": "latest"}

    def test_effective_content_filter_returns_input_for_empty_values(self) -> None:
        queryset = mock.Mock()

        result = EffectiveContentFilter(lookup_expr="icontains").filter(queryset, "   ")

        assert result is queryset
        queryset.filter.assert_not_called()
