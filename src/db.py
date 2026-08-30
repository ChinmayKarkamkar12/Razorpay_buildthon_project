"""Database connection helper. Secrets come from the environment only (never checked in)."""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in your "
            "Supabase connection string."
        )
    return dsn


def connect() -> psycopg.Connection:
    """Open a new connection. Caller is responsible for closing / using as context manager."""
    return psycopg.connect(get_dsn())
