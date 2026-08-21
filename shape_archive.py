# -*- coding: utf-8 -*-
"""Helpers for the cached Enexis SHAPE archive."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Direct target encoded inside the Outlook SafeLinks URL supplied by the user.
# urllib follows the Spotler redirect to the actual Enexis ZIP download.
SHAPE_DOWNLOAD_URL = (
    "https://c.spotler.com/ct/m3/k1/"
    "zDkrvaR4UkZUZyp6o0PSGxC4POg_9FI_xOzxbTnHUkUOSc4Xm32CQ3aHE1Lap9i9IlF7UL41l2WC4TKuJ80SWVYkQMGB12sHBnd3zkU7ZjNlvep7JvCy3tYhHfegmveFxwBPEj5X-6YhopUbPEuiaB6TQDBwyR_h6ggPTk7WN9t1AvCgI5edyvEI25giBn_0-wfF62StKIegwpyE40Vi8w/"
    "W4L6RuSXdp3sv7r"
)
TARGET_FOLDER = "imkl_elektriciteitskabel_e_lv_map_cable_ligging"
ARCHIVE_NAME = "enexis_open_asset_shapes.zip"
EXTRACTED_NAME = "enexis_open_asset_shapes_extracted"
EDGE_HASH_BYTES = 64 * 1024
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 90
HTTP_RETRIES = 3


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


def download_archive(destination, feedback=None):
    """Stream the remote ZIP to a sibling temp file and replace atomically."""
    folder = os.path.dirname(destination)
    fd, temp_path = tempfile.mkstemp(
        prefix="enexis_shape_download_", suffix=".zip.part", dir=folder
    )
    os.close(fd)
    try:
        for attempt in range(HTTP_RETRIES):
            try:
                request = Request(
                    SHAPE_DOWNLOAD_URL,
                    headers={
                        "User-Agent": "QGIS Enexis Kabelkoppeling SHAPE downloader",
                        "Accept": "application/zip, application/octet-stream, */*",
                    },
                )
                with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    raw_size = response.headers.get("Content-Length")
                    try:
                        total_size = int(raw_size) if raw_size else 0
                    except ValueError:
                        total_size = 0
                    if total_size:
                        free = shutil.disk_usage(folder).free
                        required = total_size + max(1024 * 1024 * 1024, total_size // 2)
                        if free < required:
                            raise ShapeArchiveError(
                                "Onvoldoende vrije schijfruimte voor de Enexis SHAPE-download."
                            )
                    downloaded = 0
                    with open(temp_path, "wb") as handle:
                        while True:
                            if feedback is not None and feedback.isCanceled():
                                raise ShapeArchiveError("SHAPE-download geannuleerd.")
                            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if feedback is not None and total_size:
                                feedback.setProgressText(
                                    "Enexis SHAPE ZIP downloaden: {0:.0f}/{1:.0f} MB...".format(
                                        downloaded / 1048576, total_size / 1048576
                                    )
                                )
                break
            except ShapeArchiveError:
                raise
            except (HTTPError, URLError, OSError) as exc:
                if attempt + 1 >= HTTP_RETRIES:
                    raise ShapeArchiveError(
                        "Enexis SHAPE ZIP downloaden mislukt: " + str(exc)
                    ) from exc
                time.sleep(2**attempt)

        if not zipfile.is_zipfile(temp_path):
            raise ShapeArchiveError(
                "De download via de Enexis-link is geen geldige ZIP. De downloadlink kan gewijzigd of verlopen zijn."
            )
        os.replace(temp_path, destination)
        return destination
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
