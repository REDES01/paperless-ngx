"""
Connection helper for the data-stack Postgres.

This is a SECOND database, separate from Paperless's own Django DB.
It lives in the data team's stack (paperless_data) on the shared
docker network paperless_ml_net and holds the ML schema:
    documents, document_pages, handwritten_regions,
    htr_corrections, query_sessions, search_feedback.

We deliberately do NOT use Django's multi-DB ORM here. The schema is
owned by the data team and managed by their init_sql/, not by Django
migrations. Raw psycopg keeps the boundary clean.
"""

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def _conninfo():
    host     = os.environ.get("PAPERLESS_ML_DBHOST", "postgres")
    port     = os.environ.get("PAPERLESS_ML_DBPORT", "5432")
    dbname   = os.environ.get("PAPERLESS_ML_DBNAME", "paperless")
    user     = os.environ.get("PAPERLESS_ML_DBUSER", "user")
    password = os.environ.get("PAPERLESS_ML_DBPASSWORD", "paperless_postgres")
    return (
        f"host={host} port={port} dbname={dbname} "
        f"user={user} password={password} connect_timeout=5"
    )


@contextmanager
def ml_cursor():
    """
    Yield a psycopg cursor against the data-stack Postgres.
    Returns dict-style rows. Commits on clean exit, rolls back on exception.
    """
    with psycopg.connect(_conninfo()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur
