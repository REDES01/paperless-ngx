"""
ML integration views for Paperless-ngx.

Three endpoints, all behind Paperless's normal session/login auth:

  GET  /api/ml/htr/queue/         -> regions flagged for review, grouped by document
  POST /api/ml/htr/corrections/   -> save a user's correction of an HTR region
  POST /api/ml/search/feedback/   -> save a click/thumbs feedback signal

All three read/write the data-stack Postgres via paperless_ml.db.ml_cursor,
NOT Paperless's own DB.
"""

import json
import logging
import os
import uuid
from datetime import datetime

import psycopg
import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from paperless_ml.db import ml_cursor

log = logging.getLogger("paperless_ml")


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}")


def _err(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _isoformat(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# GET /api/ml/htr/queue/
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET"])
def htr_queue(request):
    """
    Return regions where the HTR model flagged low confidence and the user has
    not yet submitted a correction. Grouped by document for the UI.
    """
    sql = """
        SELECT
            d.id            AS document_id,
            d.filename      AS title,
            d.uploaded_at   AS uploaded_at,
            r.id            AS region_id,
            p.id            AS page_id,
            r.crop_s3_url   AS crop_s3_url,
            r.htr_output    AS htr_output,
            r.htr_confidence AS htr_confidence,
            p.htr_flagged   AS page_flagged
        FROM handwritten_regions r
        JOIN document_pages p ON p.id = r.page_id
        JOIN documents d      ON d.id = p.document_id
        WHERE NOT EXISTS (
              SELECT 1 FROM htr_corrections c WHERE c.region_id = r.id
          )
          AND d.deleted_at IS NULL
        -- Flag-first ordering: low-confidence (flagged) regions bubble up,
        -- but non-flagged regions still appear so the reviewer can correct
        -- anything wrong — even when the model was confident-but-wrong.
        ORDER BY p.htr_flagged DESC, d.uploaded_at DESC, r.htr_confidence ASC
        LIMIT 200;
    """
    try:
        with ml_cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except psycopg.Error as exc:
        log.exception("htr_queue query failed")
        return _err(f"data-stack postgres unavailable: {exc}", status=503)

    grouped: dict[str, dict] = {}
    for row in rows:
        doc_id = str(row["document_id"])
        if doc_id not in grouped:
            grouped[doc_id] = {
                "document_id": doc_id,
                "title": row["title"],
                "uploaded_at": _isoformat(row["uploaded_at"]),
                "regions": [],
            }
        grouped[doc_id]["regions"].append({
            "region_id": str(row["region_id"]),
            "page_id": str(row["page_id"]),
            "crop_s3_url": row["crop_s3_url"],
            "htr_output": row["htr_output"] or "",
            "htr_confidence": float(row["htr_confidence"]) if row["htr_confidence"] is not None else 0.0,
            "page_flagged":   bool(row["page_flagged"]),
        })

    return JsonResponse(list(grouped.values()), safe=False)


# ---------------------------------------------------------------------------
# POST /api/ml/htr/corrections/
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["POST"])
@csrf_exempt
def htr_corrections(request):
    """
    Save a user correction. Payload (matches htr_corrections schema):
        { region_id, document_id, original_text, corrected_text, opted_in }
    """
    try:
        body = _json_body(request)
    except ValueError as exc:
        return _err(str(exc))

    region_id = body.get("region_id")
    corrected_text = body.get("corrected_text")
    if not region_id or corrected_text is None:
        return _err("region_id and corrected_text are required")

    user_id = None  # ML schema uses UUIDs; Paperless uses ints. Skip linkage for now.
    payload = (
        str(uuid.uuid4()),
        region_id,
        user_id,
        body.get("original_text"),
        corrected_text,
        bool(body.get("opted_in", True)),
    )
    sql = """
        INSERT INTO htr_corrections
            (id, region_id, user_id, original_text, corrected_text, opted_in)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, corrected_at;
    """
    try:
        with ml_cursor() as cur:
            cur.execute(sql, payload)
            row = cur.fetchone()
    except psycopg.Error as exc:
        log.exception("htr_corrections insert failed")
        return _err(f"insert failed: {exc}", status=500)

    return JsonResponse({
        "id": str(row["id"]),
        "corrected_at": _isoformat(row["corrected_at"]),
    }, status=201)


# ---------------------------------------------------------------------------
# POST /api/ml/search/feedback/
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["POST"])
@csrf_exempt
def search_feedback(request):
    """
    Save a click / thumbs_up / thumbs_down feedback event.
    Payload: { session_id, document_id, feedback_type }

    The data design doc requires search_feedback.session_id to reference
    query_sessions.id. Until Paperless owns the search path, we upsert a
    placeholder session row so the FK is satisfiable.
    """
    try:
        body = _json_body(request)
    except ValueError as exc:
        return _err(str(exc))

    session_id    = body.get("session_id")
    document_id   = body.get("document_id")
    feedback_type = body.get("feedback_type")

    if not (session_id and document_id and feedback_type):
        return _err("session_id, document_id, and feedback_type are required")
    if feedback_type not in ("click", "thumbs_up", "thumbs_down"):
        return _err("feedback_type must be one of click, thumbs_up, thumbs_down")

    upsert_session = """
        INSERT INTO query_sessions (id, query_text, user_id, is_test_account, result_doc_ids)
        VALUES (%s, %s, NULL, FALSE, ARRAY[]::uuid[])
        ON CONFLICT (id) DO NOTHING;
    """
    insert_feedback = """
        INSERT INTO search_feedback (id, session_id, document_id, feedback_type)
        VALUES (%s, %s, %s, %s)
        RETURNING id, created_at;
    """

    try:
        with ml_cursor() as cur:
            cur.execute(upsert_session, (session_id, "(ui session)"))
            cur.execute(insert_feedback, (str(uuid.uuid4()), session_id, document_id, feedback_type))
            row = cur.fetchone()
    except psycopg.Error as exc:
        log.exception("search_feedback insert failed")
        return _err(f"insert failed: {exc}", status=500)

    return JsonResponse({
        "id": str(row["id"]),
        "created_at": _isoformat(row["created_at"]),
    }, status=201)

# ───────────────────────────────────────────────────────────────
# Phase 5: /ml-api proxy to the ML serving layer (Yikai's FastAPI)
# ───────────────────────────────────────────────────────────────
#
# Browsers cannot reach the FastAPI container on paperless_ml_net directly,
# and the FastAPI service has no auth of its own. This view proxies requests
# from /ml-api/<path> through Paperless (which enforces login) to the serving
# FastAPI container on the shared Docker network.
#
# The UI calls /ml-api/predict/search and /ml-api/health; the view forwards
# the method + body + query string verbatim and passes the response back.

ML_SERVING_URL = os.environ.get(
    "PAPERLESS_ML_SERVING_URL",
    "http://fastapi_server:8000",
).rstrip("/")
ML_SERVING_TIMEOUT = int(os.environ.get("PAPERLESS_ML_SERVING_TIMEOUT", "30"))

# Only forward specific endpoints. Blocking arbitrary paths prevents the proxy
# from being used as a generic SSRF vector for other services on the network.
_ML_SERVING_ALLOWED_PATHS = {
    "health",
    "predict/htr",
    "predict/search",
}


@login_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def ml_serving_proxy(request, path: str):
    """
    Forward /ml-api/<path> to ML_SERVING_URL/<path>.
    Requires an authenticated Paperless session; login_required enforces this.
    """
    if path not in _ML_SERVING_ALLOWED_PATHS:
        return _err(f"path not allowed: {path}", status=404)

    url = f"{ML_SERVING_URL}/{path}"
    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            params=request.GET.dict() or None,
            data=request.body or None,
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            timeout=ML_SERVING_TIMEOUT,
        )
    except requests.Timeout:
        log.warning("ml_api: timeout to %s after %ds", url, ML_SERVING_TIMEOUT)
        return JsonResponse({"error": "serving timeout"}, status=504)
    except requests.ConnectionError as exc:
        log.warning("ml_api: cannot reach %s: %s", url, exc)
        return JsonResponse({"error": "serving unavailable"}, status=502)
    except requests.RequestException as exc:
        log.exception("ml_api: unexpected error to %s: %s", url, exc)
        return JsonResponse({"error": "serving error"}, status=502)

    return HttpResponse(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )
