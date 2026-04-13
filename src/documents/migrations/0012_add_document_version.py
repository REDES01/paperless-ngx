from __future__ import annotations

import django.core.validators
import django.db.models
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0011_alter_workflowaction_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentVersion",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "version_number",
                    models.PositiveSmallIntegerField(
                        help_text="Sequential version number within this document, starting at 1.",
                        verbose_name="version number",
                    ),
                ),
                (
                    "version_label",
                    models.CharField(
                        blank=True,
                        help_text="Optional short label for this version.",
                        max_length=64,
                        null=True,
                        verbose_name="version label",
                    ),
                ),
                (
                    "added",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="added",
                    ),
                ),
                (
                    "checksum",
                    models.CharField(
                        editable=False,
                        help_text="SHA-256 checksum of the original file for this version.",
                        max_length=64,
                        verbose_name="checksum",
                    ),
                ),
                (
                    "archive_checksum",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=64,
                        null=True,
                        verbose_name="archive checksum",
                    ),
                ),
                (
                    "content",
                    models.TextField(
                        blank=True,
                        help_text="OCR text content of this version.",
                        verbose_name="content",
                    ),
                ),
                (
                    "page_count",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                        verbose_name="page count",
                    ),
                ),
                (
                    "mime_type",
                    models.CharField(
                        editable=False,
                        max_length=256,
                        verbose_name="mime type",
                    ),
                ),
                (
                    "original_filename",
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=1024,
                        null=True,
                        verbose_name="original filename",
                    ),
                ),
                (
                    "filename",
                    models.FilePathField(
                        default=None,
                        editable=False,
                        help_text="Stored filename for this version's original file.",
                        max_length=1024,
                        null=True,
                        verbose_name="filename",
                    ),
                ),
                (
                    "archive_filename",
                    models.FilePathField(
                        default=None,
                        editable=False,
                        max_length=1024,
                        null=True,
                        verbose_name="archive filename",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="documents.document",
                        verbose_name="document",
                    ),
                ),
            ],
            options={
                "verbose_name": "document version",
                "verbose_name_plural": "document versions",
                "ordering": ["-version_number"],
            },
        ),
        migrations.AddConstraint(
            model_name="documentversion",
            constraint=models.UniqueConstraint(
                fields=("document", "version_number"),
                name="documents_documentversion_doc_number_uniq",
            ),
        ),
    ]
