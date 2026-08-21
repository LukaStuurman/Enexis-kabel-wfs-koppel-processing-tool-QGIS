# -*- coding: utf-8 -*-
"""Reusable disk-backed index for the nationwide Enexis CSV export."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile

from .matching import normalize_label, parse_decimal


INDEX_SCHEMA_VERSION = "1"
INDEX_PREFIX = "enexis_csv_index_"
EDGE_HASH_BYTES = 64 * 1024
INSERT_BATCH_SIZE = 5000
SQLITE_QUERY_CHUNK = 400


class CsvIndexError(RuntimeError):
    pass


class CsvIndex:
    def __init__(self, path, connection, fieldnames, stats, reused):
        self.path = path
        self.connection = connection
        self.fieldnames = list(fieldnames)
        self.stats = dict(stats)
        self.reused = bool(reused)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def rows_for_labels(self, labels):
        labels = sorted(set(label for label in labels if label))
        if not labels:
            return []

        rows = []
        for position in range(0, len(labels), SQLITE_QUERY_CHUNK):
            chunk = labels[position : position + SQLITE_QUERY_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT row_number, label, length_m, length_error, values_json "
                "FROM csv_rows WHERE label IN ({0}) ORDER BY row_number"
            ).format(placeholders)
            for row_number, label, length_m, length_error, values_json in (
                self.connection.execute(query, chunk)
            ):
                values = json.loads(values_json)
                rows.append(
                    {
                        "row_number": row_number,
                        "values": dict(zip(self.fieldnames, values)),
                        "label": label,
                        "length_m": length_m,
                        "length_error": length_error,
                    }
                )
        rows.sort(key=lambda row: row["row_number"])
        return rows


def _target_folder(cache_folder):
    folder = (cache_folder or "").strip()
    if not folder or folder.upper() == "TEMPORARY_OUTPUT":
        folder = os.path.join(tempfile.gettempdir(), "enexis_kabel_csv_cache")
    os.makedirs(folder, exist_ok=True)
    return folder


def _source_edge_hash(path, size):
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        digest.update(handle.read(EDGE_HASH_BYTES))
        if size > EDGE_HASH_BYTES:
            handle.seek(max(0, size - EDGE_HASH_BYTES))
            digest.update(handle.read(EDGE_HASH_BYTES))
    return digest.hexdigest()


def _source_signature(path):
    absolute = os.path.abspath(path)
    stat = os.stat(absolute)
    return {
        "source_path": absolute,
        "source_size": str(stat.st_size),
        "source_mtime_ns": str(stat.st_mtime_ns),
        "source_edge_sha256": _source_edge_hash(absolute, stat.st_size),
    }


def _index_path(csv_path, cache_folder):
    folder = _target_folder(cache_folder)
    key = hashlib.sha256(
        os.path.normcase(os.path.abspath(csv_path)).encode("utf-8")
    ).hexdigest()[:20]
    return os.path.join(folder, INDEX_PREFIX + key + ".sqlite")


def _configure_build_connection(connection):
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-16384")
    connection.execute("PRAGMA mmap_size=0")


def _configure_read_connection(connection):
    # Keep reuse cheap: no full PRAGMA integrity/quick_check on a ~700 MB index.
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-16384")
    connection.execute("PRAGMA mmap_size=0")


def _read_meta(connection):
    try:
        return dict(connection.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        return {}


def _stats_from_meta(meta):
    def as_int(name):
        try:
            return int(meta.get(name, "0"))
        except (TypeError, ValueError):
            return 0

    try:
        sample_labels = json.loads(meta.get("sample_labels_json", "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        sample_labels = []
    return {
        "total_rows": as_int("total_rows"),
        "valid_unique_labels": as_int("valid_unique_labels"),
        "invalid_lengths": as_int("invalid_lengths"),
        "empty_labels": as_int("empty_labels"),
        "sample_labels": sample_labels,
    }


def _valid_existing_index(index_path, signature):
    if not os.path.exists(index_path):
        return None
    connection = None
    try:
        connection = sqlite3.connect(index_path)
        _configure_read_connection(connection)
        meta = _read_meta(connection)
        expected = dict(signature)
        expected["schema_version"] = INDEX_SCHEMA_VERSION
        if any(meta.get(key) != value for key, value in expected.items()):
            connection.close()
            return None
        fieldnames = json.loads(meta["fieldnames_json"])
        if not isinstance(fieldnames, list) or not fieldnames:
            connection.close()
            return None
        # Constant-time structural guard. A deep integrity check would defeat
        # the purpose of instant reuse for small extents.
        connection.execute("SELECT row_number FROM csv_rows LIMIT 1").fetchone()
        return CsvIndex(
            index_path,
            connection,
            fieldnames,
            _stats_from_meta(meta),
            reused=True,
        )
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        if connection is not None:
            connection.close()
        return None


def _remove_sqlite_files(path):
    for candidate in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(candidate)
        except OSError:
            pass


def _detect_delimiter(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        return ";"


def _schema(path, label_field, length_field):
    delimiter = _detect_delimiter(path)
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        fieldnames = reader.fieldnames or []
    missing = [name for name in (label_field, length_field) if name not in fieldnames]
    if missing:
        raise CsvIndexError(
            "CSV mist verplichte kolom(men): " + ", ".join(missing)
        )
    return delimiter, fieldnames


def _check_free_space(csv_path, target_folder):
    csv_size = os.path.getsize(csv_path)
    free_bytes = shutil.disk_usage(target_folder).free
    # The measured 500 MB source produced a ~700 MB SQLite index. Three times
    # the CSV size leaves room for the index plus temporary B-tree construction.
    required_bytes = max(2 * 1024 * 1024 * 1024, csv_size * 3)
    if free_bytes < required_bytes:
        raise CsvIndexError(
            "Onvoldoende vrije ruimte voor de herbruikbare CSV-index. Voor deze "
            "CSV is minimaal {0:.1f} GB vrij aanbevolen; beschikbaar is {1:.1f} "
            "GB.".format(required_bytes / 1073741824, free_bytes / 1073741824)
        )


def open_csv_index(
    csv_path,
    cache_folder,
    label_field,
    length_field,
    feedback=None,
):
    """Open a valid reusable index, or build one atomically when required."""
    csv_path = os.path.abspath(csv_path)
    signature = _source_signature(csv_path)
    index_path = _index_path(csv_path, cache_folder)

    existing = _valid_existing_index(index_path, signature)
    if existing is not None:
        if feedback is not None:
            feedback.pushInfo(
                "Bestaande CSV-index hergebruikt: {0:.1f} MB.".format(
                    os.path.getsize(index_path) / 1048576
                )
            )
        return existing

    target_folder = os.path.dirname(index_path)
    _check_free_space(csv_path, target_folder)
    _remove_sqlite_files(index_path)
    delimiter, fieldnames = _schema(csv_path, label_field, length_field)

    descriptor, building_path = tempfile.mkstemp(
        prefix=".enexis_csv_index_build_",
        suffix=".sqlite",
        dir=target_folder,
    )
    os.close(descriptor)
    connection = None
    replaced = False
    try:
        if feedback is not None:
            feedback.setProgressText(
                "Herbruikbare CSV-index bouwen (eenmalig voor deze CSV-versie)..."
            )
        connection = sqlite3.connect(building_path)
        _configure_build_connection(connection)
        connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE csv_rows (
                row_number INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                length_m REAL,
                length_error TEXT NOT NULL,
                values_json TEXT NOT NULL
            )
            """
        )
        insert_sql = (
            "INSERT INTO csv_rows "
            "(row_number, label, length_m, length_error, values_json) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        pending = []
        total_rows = 0
        invalid_lengths = 0
        empty_labels = 0
        sample_labels = set()

        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            for row_number, row in enumerate(reader, start=2):
                if feedback is not None and feedback.isCanceled():
                    raise CsvIndexError("CSV-indexering geannuleerd.")

                total_rows += 1
                label = normalize_label(row.get(label_field))
                if not label:
                    empty_labels += 1
                elif len(sample_labels) < 12:
                    sample_labels.add(label)

                raw_length = row.get(length_field)
                try:
                    length_m = round(parse_decimal(raw_length), 2)
                    length_error = ""
                except (TypeError, ValueError):
                    length_m = None
                    length_error = "ONGELDIGE_CSV_LENGTE"
                    invalid_lengths += 1

                values = [(row.get(name) or "") for name in fieldnames]
                pending.append(
                    (
                        row_number,
                        label,
                        length_m,
                        length_error,
                        json.dumps(
                            values,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
                if len(pending) >= INSERT_BATCH_SIZE:
                    connection.executemany(insert_sql, pending)
                    pending.clear()

                if total_rows % 100000 == 0:
                    connection.commit()
                    if feedback is not None:
                        feedback.pushInfo(
                            "CSV-index: {0} rijen verwerkt...".format(total_rows)
                        )

        if pending:
            connection.executemany(insert_sql, pending)
            pending.clear()
        connection.commit()

        if feedback is not None:
            feedback.setProgressText("CSV-labelindex opbouwen...")
        connection.execute("CREATE INDEX idx_csv_rows_label ON csv_rows(label)")
        connection.commit()
        valid_unique_labels = connection.execute(
            "SELECT COUNT(DISTINCT label) FROM csv_rows "
            "WHERE label <> '' AND length_m IS NOT NULL"
        ).fetchone()[0]

        stats = {
            "total_rows": total_rows,
            "valid_unique_labels": valid_unique_labels,
            "invalid_lengths": invalid_lengths,
            "empty_labels": empty_labels,
            "sample_labels": sorted(sample_labels),
        }
        meta = dict(signature)
        meta.update(
            {
                "schema_version": INDEX_SCHEMA_VERSION,
                "fieldnames_json": json.dumps(
                    fieldnames, ensure_ascii=False, separators=(",", ":")
                ),
                "total_rows": str(total_rows),
                "valid_unique_labels": str(valid_unique_labels),
                "invalid_lengths": str(invalid_lengths),
                "empty_labels": str(empty_labels),
                "sample_labels_json": json.dumps(
                    stats["sample_labels"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)", sorted(meta.items())
        )
        connection.commit()
        connection.close()
        connection = None

        os.replace(building_path, index_path)
        replaced = True
        connection = sqlite3.connect(index_path)
        _configure_read_connection(connection)
        if feedback is not None:
            feedback.pushInfo(
                "Nieuwe herbruikbare CSV-index gereed: {0} rijen, {1} unieke "
                "geldige labels, {2:.1f} MB.".format(
                    total_rows,
                    valid_unique_labels,
                    os.path.getsize(index_path) / 1048576,
                )
            )
        return CsvIndex(index_path, connection, fieldnames, stats, reused=False)
    except Exception:
        if connection is not None:
            connection.close()
        _remove_sqlite_files(building_path)
        if replaced:
            _remove_sqlite_files(index_path)
        raise
