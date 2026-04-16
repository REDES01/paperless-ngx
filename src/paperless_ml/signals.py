"""
Django signal handlers for the paperless_ml app.

Currently:
  - On Document creation (post_save with created=True), publish a
    paperless.uploads event to Redpanda so downstream ML services
    (HTR preprocessing, document indexing) can react.
"""

import logging
import os
from datetime import datetime

from django.db.models.signals import post_save
from django.dispatch import receiver

from documents.models import Document

from paperless_ml.producer import publish

log = logging.getLogger("paperless_ml")

UPLOADS_TOPIC = os.environ.get("PAPERLESS_ML_KAFKA_TOPIC", "paperless.uploads")


def _to_iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    # documents.Document.created is a DateField (no time component)
    return dt.isoformat()


@receiver(post_save, sender=Document, dispatch_uid="paperless_ml_document_created")
def on_document_created(sender, instance: Document, created: bool, **kwargs) -> None:
    """
    Publish paperless.uploads on new Document rows.
    Edits/updates are ignored — we only care about the upload moment.
    """
    if not created:
        return

    event = {
        "paperless_doc_id": instance.pk,
        "title": instance.title or "",
        "page_count": instance.page_count or 1,
        "uploaded_at": _to_iso(instance.created),
        "source": "user_upload",
    }

    ok = publish(UPLOADS_TOPIC, event)
    if ok:
        log.info(
            "paperless_ml: published upload event for doc id=%s (page_count=%s)",
            instance.pk,
            event["page_count"],
        )
