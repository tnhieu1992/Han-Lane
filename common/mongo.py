"""Shared MongoDB client. One client per process, zstd wire compression."""
from __future__ import annotations

from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

from common.settings import settings


@lru_cache
def get_client(appname: str = "hanlane") -> MongoClient:
    s = settings()
    if not s.mongodb_uri:
        raise RuntimeError("MONGODB_URI is not set (see .env.example)")
    return MongoClient(
        s.mongodb_uri,
        appname=appname,
        compressors="zstd",
        serverSelectionTimeoutMS=8000,
        tz_aware=True,
    )


def get_db(appname: str = "hanlane") -> Database:
    return get_client(appname)[settings().mongodb_db]
