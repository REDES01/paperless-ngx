"""
Django signal handlers for the paperless_ml app.

Fires on:
  - Document creation       -> publish paperless.uploads (existing)
  - Document soft-delete    -> clean up ML Postgres + Qdrant (new)
  - Document hard-delete    -> clean up ML Postgres + Qdrant (new)
"""

import logging
import os
from datetime import datetime

import requests

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from documents.models import Document

from paperless_ml.db import ml_cursor
from paperless_ml.producer import publish

log = logging.getLogger("paperless_ml")

UPLOADS_TOPIC = os.environ.get("PAPERLESS_ML_KAFKA_TOPIC", "paperless.uploads")

# Qdrant cleanup config. Uses the REST API directly to avoid adding a
# qdrant-client dependency to the Paperless image.
QDRANT_HOST       = os.environ.get("PAPERLESS_ML_QDRANT_HOST", "qdrant")
QDRANT_PORT       = int(os.environ.get("PAPERLESS_ML_QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("PAPERLESS_ML_QDRANT_COLLECTION", "document_chunks")
QDRANT_TIMEOUT    = float(os.environ.get("PAPERLESS_ML_QDRANT_TIMEOUT", "5.0"))


def _to_iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    # documents.Document.created is a DateField (no time component)
    return dt.isoformat()


# ───────────────────────────────────────────────────────────────
# Upload path (unchanged)
# ───────────────────────────────────────────────────────────────
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


# ───────────────────────────────────────────────────────────────
# Delete cleanup path
# ───────────────────────────────────────────────────────────────

def _soft_delete_ml_document(paperless_doc_id: int) -> None:
    """Mark ML documents row deleted so htr_queue and search exclude it."""
    with ml_cursor() as cur:
        cur.execute(
            "UPDATE documents SET deleted_at = NOW() "
            "WHERE paperless_doc_id = %s AND deleted_at IS NULL",
            (paperless_doc_id,),
        )


def _delete_qdrant_points(paperless_doc_id: int) -> None:
    """Remove all Qdrant points for this Paperless doc id via REST API."""
    url = (
        f"http://{QDRANT_HOST}:{QDRANT_PORT}"
        f"/collections/{QDRANT_COLLECTION}/points/delete"
    )
    body = {
        "filter": {
            "must": [
                {"key": "paperless_doc_id", "match": {"value": paperless_doc_id}}
            ]
        }
    }
    resp = requests.post(url, json=body, timeout=QDRANT_TIMEOUT)
    resp.raise_for_status()


def _cleanup_ml_data(paperless_doc_id: int) -> None:
    """
    Best-effort cleanup across ML Postgres + Qdrant. Errors are logged but
    do not propagate — we never want ML cleanup failure to block a
    Paperless document deletion.
    """
    try:
        _soft_delete_ml_document(paperless_doc_id)
        log.info(
            "paperless_ml: marked ML document deleted (paperless_doc_id=%s)",
            paperless_doc_id,
        )
    except Exception as exc:
        log.warning(
            "paperless_ml: soft-delete ML doc failed for paperless_doc_id=%s: %s",
            paperless_doc_id, exc,
        )

    try:
        _delete_qdrant_points(paperless_doc_id)
        log.info(
            "paperless_ml: deleted qdrant points for paperless_doc_id=%s",
            paperless_doc_id,
        )
    except Exception as exc:
        log.warning(
            "paperless_ml: qdrant cleanup failed for paperless_doc_id=%s: %s",
            paperless_doc_id, exc,
        )


@receiver(post_save, sender=Document, dispatch_uid="paperless_ml_document_soft_deleted")
def on_document_soft_deleted(sender, instance: Document, created: bool, **kwargs) -> None:
    """
    Fires when Paperless soft-deletes a document via django-soft-delete.
    Its SoftDeleteModel.delete() sets deleted_at and calls save(), which
    triggers post_save with created=False and deleted_at!=None.
    """
    if created:
        return  # upload path
    if getattr(instance, "deleted_at", None) is None:
        return  # regular update, not a delete
    _cleanup_ml_data(instance.pk)


@receiver(post_delete, sender=Document, dispatch_uid="paperless_ml_document_hard_deleted")
def on_document_hard_deleted(sender, instance: Document, **kwargs) -> None:
    """
    Fires on hard delete (e.g., 'empty trash' in Paperless UI). Idempotent
    with the soft-delete handler: if the row is already marked deleted, the
    UPDATE is a no-op and Qdrant delete-by-filter is a no-op too.
    """
    _cleanup_ml_data(instance.pk)
