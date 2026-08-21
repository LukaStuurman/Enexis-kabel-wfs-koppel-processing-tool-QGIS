# -*- coding: utf-8 -*-
"""Reusable SQLite index helpers for the nationwide Enexis WFS layer."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone


INDEX_SCHEMA_VERSION = "1"
INDEX_PREFIX = "enexis_wfs_index_"


class WfsIndexError(RuntimeError):
    pass


class WfsIndex:
    def __init__(self, path, connection, meta, reused):
        self.path = path
        self.connection = connection
        self.meta = dict(meta)
        self.reused = bool(reused)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def target_folder(cache_folder):
    folder = (cache_folder or "").strip()
    if not folder or folder.upper() == "TEMPORARY_OUTPUT":
        folder = os.path.join(tempfile.gettempdir(), "enexis_kabel_csv_cache")
    os.makedirs(folder, exist_ok=True)
    return folder


def index_path(cache_folder, source_url, type_name):
    folder = target_folder(cache_folder)
    key = hashlib.sha256(
        (str(source_url).strip() + "\n" + str(type_name).strip()).encode("utf-8")
    ).hexdigest()[:20]
    return os.path.join(folder, INDEX_PREFIX + key + ".sqlite")


def configure_connection(connection):
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA mmap_size=0")


def expected_meta(source_url, type_name, label_field, geometry_field, srs_name):
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_url": str(source_url),
        "type_name": str(type_name),
        "label_field": str(label_field),
        "geometry_field": str(geometry_field),
        "srs_name": str(srs_name),
    }


def _read_meta(connection):
    try:
        return dict(connection.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        return {}


def open_existing(
    cache_folder,
    source_url,
    type_name,
    label_field,
    geometry_field,
    srs_name,
):
    path = index_path(cache_folder, source_url, type_name)
    if not os.path.exists(path):
        return None

    connection = None
    try:
        connection = sqlite3.connect(path)
        configure_connection(connection)
        meta = _read_meta(connection)
        expected = expected_meta(
            source_url, type_name, label_field, geometry_field, srs_name
        )
        if any(meta.get(key) != value for key, value in expected.items()):
            connection.close()
            return None
        connection.execute("SELECT 1 FROM wfs_rows LIMIT 1").fetchone()
        connection.execute("SELECT 1 FROM wfs_labels LIMIT 1").fetchone()
        return WfsIndex(path, connection, meta, reused=True)
    except (sqlite3.Error, OSError):
        if connection is not None:
            connection.close()
        return None


def _remove_sidecars(path):
    for candidate in (path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(candidate)
        except OSError:
            pass


def _remove_all(path):
    try:
        os.remove(path)
    except OSError:
        pass
    _remove_sidecars(path)


class WfsIndexBuilder:
    def __init__(
        self,
        cache_folder,
        source_url,
        type_name,
        label_field,
        geometry_field,
        srs_name,
    ):
        self.final_path = index_path(cache_folder, source_url, type_name)
        folder = os.path.dirname(self.final_path)
        free_bytes = shutil.disk_usage(folder).free
        if free_bytes < 3 * 1024 * 1024 * 1024:
            raise WfsIndexError(
                "Onvoldoende vrije ruimte voor de landelijke WFS-index. "
                "Minimaal 3 GB vrije ruimte is vereist."
            )

        fd, self.temp_path = tempfile.mkstemp(
            prefix="enexis_wfs_build_", suffix=".sqlite", dir=folder
        )
        os.close(fd)
        self.connection = sqlite3.connect(self.temp_path)
        configure_connection(self.connection)
        self.connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute(
            """
            CREATE TABLE wfs_rows (
                source_key TEXT PRIMARY KEY,
                source_fid TEXT NOT NULL,
                label TEXT NOT NULL,
                length_m REAL NOT NULL,
                geometry_wkb BLOB NOT NULL
            )
            """
        )
        self.connection.execute("CREATE TABLE wfs_labels (label TEXT PRIMARY KEY)")
        self._base_meta = expected_meta(
            source_url, type_name, label_field, geometry_field, srs_name
        )
        self.inserted_features = 0

    def insert_records(self, records):
        if not records:
            return 0
        before_rows = self.connection.total_changes
        self.connection.executemany(
            "INSERT OR IGNORE INTO wfs_rows "
            "(source_key, source_fid, label, length_m, geometry_wkb) "
            "VALUES (?, ?, ?, ?, ?)",
            records,
        )
        inserted_rows = self.connection.total_changes - before_rows
        self.connection.executemany(
            "INSERT OR IGNORE INTO wfs_labels(label) VALUES (?)",
            [(record[2],) for record in records if record[2]],
        )
        self.inserted_features += inserted_rows
        return inserted_rows

    def commit(self):
        self.connection.commit()

    def finalize(self, download_format, raw_feature_count, extra_meta=None):
        self.connection.execute("CREATE INDEX idx_wfs_rows_label ON wfs_rows(label)")
        feature_count = self.connection.execute("SELECT COUNT(*) FROM wfs_rows").fetchone()[0]
        label_count = self.connection.execute("SELECT COUNT(*) FROM wfs_labels").fetchone()[0]
        meta = dict(self._base_meta)
        meta.update(
            {
                "built_at_utc": datetime.now(timezone.utc).isoformat(),
                "download_format": str(download_format),
                "raw_feature_count": str(int(raw_feature_count)),
                "feature_count": str(int(feature_count)),
                "label_count": str(int(label_count)),
            }
        )
        if extra_meta:
            meta.update({str(k): str(v) for k, v in extra_meta.items()})
        self.connection.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            sorted(meta.items()),
        )
        self.connection.commit()
        self.connection.close()
        self.connection = None

        _remove_sidecars(self.final_path)
        os.replace(self.temp_path, self.final_path)
        self.temp_path = ""
        connection = sqlite3.connect(self.final_path)
        configure_connection(connection)
        return WfsIndex(self.final_path, connection, meta, reused=False)

    def abort(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.temp_path:
            _remove_all(self.temp_path)
            self.temp_path = ""


def tile_bounds(bounds, tile_size):
    xmin, ymin, xmax, ymax = [float(value) for value in bounds]
    size = float(tile_size)
    if size <= 0:
        raise ValueError("tile_size moet groter dan nul zijn")
    tiles = []
    x = xmin
    tile_id = 0
    while x < xmax:
        y = ymin
        x2 = min(x + size, xmax)
        while y < ymax:
            y2 = min(y + size, ymax)
            tile_id += 1
            tiles.append((tile_id, x, y, x2, y2))
            y = y2
        x = x2
    return tiles
