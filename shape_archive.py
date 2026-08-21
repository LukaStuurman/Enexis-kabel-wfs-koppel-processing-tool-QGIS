# -*- coding: utf-8 -*-
"""Helpers for the cached Enexis SHAPE archive."""

from __future__ import annotations

import hashlib
import os
import tempfile

TARGET_FOLDER = "imkl_elektriciteitskabel_e_lv_map_cable_ligging"
ARCHIVE_NAME = "enexis_open_asset_shapes.zip"
EXTRACTED_NAME = "enexis_open_asset_shapes_extracted"
EDGE_HASH_BYTES = 64 * 1024


class ShapeArchiveError(RuntimeError):
    pass


def cache_root(cache_folder):
    root = (cache_folder or "").strip()
    if not root or root.upper() == "TEMPORARY_OUTPUT":
        root = os.path.join(tempfile.gettempdir(), "enexis_kabel_csv_cache")
    root = os.path.join(root, "enexis_shape_source")
    os.makedirs(root, exist_ok=True)
    return root


def archive_paths(cache_folder):
    root = cache_root(cache_folder)
    return os.path.join(root, ARCHIVE_NAME), os.path.join(root, EXTRACTED_NAME)


def archive_fingerprint(path):
    size = os.path.getsize(path)
    stat = os.stat(path)
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        digest.update(handle.read(EDGE_HASH_BYTES))
        if size > EDGE_HASH_BYTES:
            handle.seek(max(0, size - EDGE_HASH_BYTES))
            digest.update(handle.read(EDGE_HASH_BYTES))
    return {
        "archive_size": str(size),
        "archive_mtime_ns": str(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
        "archive_edge_sha256": digest.hexdigest(),
    }
