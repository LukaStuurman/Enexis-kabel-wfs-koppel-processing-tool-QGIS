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

# Current Enexis/Spotler SHAPE download link supplied by the user.
SHAPE_DOWNLOAD_URL = (
    "https://c.spotler.com/ct/m3/k1/"
    "KdKQtH6B0wZjnPBT7BzqOtEsn-w1iebQF8ZDB2NGKTYcA4pq8agO-N_rqoymPoGmXmbyRWK5Y-t6tJGhGUv84awweWYQnJYsH5vDkKODg3_b1yt9gHlYGgO6bvmtvIKmr8wPsm5YhfVgEzVmNVPQ6wqVW49PJ4Twysgqdkc00ryGvq4cQnuanRps7J1UzY9JRCE_DjZR-FqZ7a2pj5ESEw/"
    "ig3SD3vmbjdIP76"
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


def _target_relative_parts(member_name):
    normalized = str(member_name or "").replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    wanted = TARGET_FOLDER.casefold()
    target_index = None
    for index, part in enumerate(parts):
        if part.casefold() == wanted:
            target_index = index
            break
    if target_index is None:
        return None
    relative = parts[target_index + 1 :]
    if not relative or any(part == ".." for part in relative):
        return None
    return relative


def _sidecars_for(path):
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0].casefold()
    suffixes = set()
    for name in os.listdir(folder):
        candidate_stem, candidate_ext = os.path.splitext(name)
        if candidate_stem.casefold() == stem:
            suffixes.add(candidate_ext.casefold())
    return suffixes


def discover_shape_files(extracted_folder):
    """Find exactly one Noord and one Zuid SHP and validate their sidecars."""
    shape_files = []
    for root, _, files in os.walk(extracted_folder):
        for name in files:
            if os.path.splitext(name)[1].casefold() == ".shp":
                shape_files.append(os.path.join(root, name))

    north = [
        path for path in shape_files
        if "noord" in os.path.basename(path).casefold()
        or "north" in os.path.basename(path).casefold()
    ]
    south = [
        path for path in shape_files
        if "zuid" in os.path.basename(path).casefold()
        or "south" in os.path.basename(path).casefold()
    ]
    if len(north) != 1 or len(south) != 1:
        names = ", ".join(sorted(os.path.basename(path) for path in shape_files)) or "(geen)"
        raise ShapeArchiveError(
            "In '{0}' moeten exact twee SHAPE-lagen staan: één met Noord en één met Zuid in de naam. "
            "Gevonden SHP-bestanden: {1}".format(TARGET_FOLDER, names)
        )
    if os.path.normcase(north[0]) == os.path.normcase(south[0]):
        raise ShapeArchiveError("Noord en Zuid verwijzen naar hetzelfde SHAPE-bestand.")

    for path in (north[0], south[0]):
        suffixes = _sidecars_for(path)
        missing = [ext for ext in (".dbf", ".shx", ".prj") if ext not in suffixes]
        if missing:
            raise ShapeArchiveError(
                "SHAPE-bestand '{0}' mist verplichte sidecar(s): {1}.".format(
                    os.path.basename(path), ", ".join(missing)
                )
            )
    return {"noord": north[0], "zuid": south[0]}


def extract_target_folder(archive_path, destination):
    """Extract only the requested IMKL folder into an atomic cache directory."""
    parent = os.path.dirname(destination)
    build_root = tempfile.mkdtemp(prefix="enexis_shape_extract_", dir=parent)
    build_target = os.path.join(build_root, TARGET_FOLDER)
    os.makedirs(build_target, exist_ok=True)
    extracted_count = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = _target_relative_parts(info.filename)
                if relative is None:
                    continue
                target = os.path.abspath(os.path.join(build_target, *relative))
                root = os.path.abspath(build_target)
                if os.path.commonpath((target, root)) != root:
                    raise ShapeArchiveError("Onveilige padnaam in de Enexis ZIP geweigerd.")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(info, "r") as source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output, DOWNLOAD_CHUNK_BYTES)
                extracted_count += 1

        if extracted_count == 0:
            raise ShapeArchiveError(
                "Map '{0}' is niet gevonden in de Enexis ZIP.".format(TARGET_FOLDER)
            )
        discover_shape_files(build_target)  # validate before replacing the good cache
        old_path = destination + ".old"
        shutil.rmtree(old_path, ignore_errors=True)
        moved_old = False
        if os.path.exists(destination):
            os.replace(destination, old_path)
            moved_old = True
        try:
            os.replace(build_target, destination)
        except Exception:
            if moved_old and not os.path.exists(destination) and os.path.exists(old_path):
                os.replace(old_path, destination)
            raise
        shutil.rmtree(old_path, ignore_errors=True)
        return discover_shape_files(destination)
    finally:
        shutil.rmtree(build_root, ignore_errors=True)


def ensure_shape_archive(cache_folder, refresh=False, feedback=None):
    """Download when needed, extract the target folder and return both SHP paths."""
    archive_path, extracted_folder = archive_paths(cache_folder)
    downloaded = False
    if refresh or not os.path.exists(archive_path):
        if feedback is not None:
            feedback.setProgressText("Enexis SHAPE ZIP automatisch downloaden...")
        download_archive(archive_path, feedback=feedback)
        downloaded = True

    if not zipfile.is_zipfile(archive_path):
        raise ShapeArchiveError(
            "De lokale Enexis SHAPE-cache bevat geen geldige ZIP. Vernieuw de SHAPE-download."
        )

    if downloaded or not os.path.isdir(extracted_folder):
        if feedback is not None:
            feedback.setProgressText(
                "Enexis ZIP uitpakken: alleen map '{0}'...".format(TARGET_FOLDER)
            )
        shape_files = extract_target_folder(archive_path, extracted_folder)
    else:
        try:
            shape_files = discover_shape_files(extracted_folder)
        except ShapeArchiveError:
            shape_files = extract_target_folder(archive_path, extracted_folder)

    return {
        "archive_path": archive_path,
        "extracted_folder": extracted_folder,
        "shape_files": shape_files,
        "fingerprint": archive_fingerprint(archive_path),
        "downloaded": downloaded,
    }
