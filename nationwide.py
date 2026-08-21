# -*- coding: utf-8 -*-
"""Fast nationwide Enexis WFS/CSV processing for QGIS 4.2."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsGeometry,
    QgsJsonUtils,
    QgsProcessing,
    QgsProcessingException,
    QgsVectorLayer,
)

from .algorithm import NO_GEOMETRY
from .matching import normalize_label, optimal_one_to_one
from .wfs_index import WfsIndexBuilder, WfsIndexError, open_existing, tile_bounds


class NationwideWfsError(RuntimeError):
    pass


class NationwideProcessor:
    """Build/reuse a persistent WFS index and match it locally to the CSV index."""

    # Covers the Dutch RD domain used by Enexis, with a small safety margin.
    NATIONWIDE_RD_BOUNDS = (-10000.0, 280000.0, 300000.0, 630000.0)
    TILE_SIZE_M = 25000.0
    TILE_PAGE_SIZE = 5000
    MIN_TILE_PAGE_SIZE = 1000
    MAX_TILE_PAGE_BYTES = 48 * 1024 * 1024
    MAX_TILE_WORKERS = 2
    MATCH_LABEL_BATCH = 50
    HTTP_TIMEOUT_SECONDS = 30
    HTTP_RETRIES = 3
    FORMAT_BENCHMARK_COUNT = 500

    def __init__(
        self,
        algorithm,
        parameters,
        context,
        feedback,
        csv_path,
        source_crs,
        cache_folder,
        total_started,
        refresh_wfs_index,
        only_matched_output,
    ):
        self.algorithm = algorithm
        self.parameters = parameters
        self.context = context
        self.feedback = feedback
        self.csv_path = csv_path
        self.source_crs = source_crs
        self.cache_folder = cache_folder
        self.total_started = total_started
        self.refresh_wfs_index = bool(refresh_wfs_index)
        self.only_matched_output = bool(only_matched_output)
        self.cancel_event = threading.Event()

    @staticmethod
    def _destination_text(value):
        if value is None:
            return ""
        sink = getattr(value, "sink", None)
        if sink is not None:
            try:
                value = sink() if callable(sink) else sink
            except Exception:
                pass
        return str(value or "").strip()

    def _require_disk_output(self, key, label):
        raw = self.parameters.get(key)
        text = self._destination_text(raw)
        if (
            raw == QgsProcessing.TEMPORARY_OUTPUT
            or not text
            or text.upper() == "TEMPORARY_OUTPUT"
            or text.lower().startswith("memory:")
        ):
            raise QgsProcessingException(
                "Landelijke modus weigert tijdelijke/geheugenuitvoer voor '{0}'. "
                "Kies expliciet een bestand op lokale schijf, bij voorkeur GeoPackage."
                .format(label)
            )

    def _cancel_if_requested(self, message="Landelijke verwerking geannuleerd."):
        if self.feedback.isCanceled():
            self.cancel_event.set()
            raise QgsProcessingException(message)

    @staticmethod
    def _read_bounded(response, limit):
        raw_size = response.headers.get("Content-Length")
        try:
            size = int(raw_size) if raw_size else 0
        except ValueError:
            size = 0
        if size > limit:
            raise NationwideWfsError(
                "WFS-response te groot: {0:.1f} MB (limiet {1:.1f} MB).".format(
                    size / 1048576, limit / 1048576
                )
            )
        data = response.read(limit + 1)
        if len(data) > limit:
            raise NationwideWfsError(
                "WFS-response overschrijdt de limiet van {0:.1f} MB.".format(
                    limit / 1048576
                )
            )
        return data

    def _request_raw(self, params, limit, accept):
        request = Request(
            self.algorithm.WFS_URL + "?" + urlencode(params),
            headers={
                "User-Agent": "QGIS Enexis Kabelkoppeling",
                "Accept": accept,
            },
        )
        for attempt in range(self.HTTP_RETRIES):
            if self.cancel_event.is_set():
                raise NationwideWfsError("Landelijke download geannuleerd.")
            try:
                with urlopen(request, timeout=self.HTTP_TIMEOUT_SECONDS) as response:
                    data = self._read_bounded(response, limit)
                    return data, str(response.headers.get("Content-Type") or "")
            except NationwideWfsError:
                raise
            except HTTPError as exc:
                try:
                    detail = exc.read(1000).decode("utf-8", errors="replace")[:500]
                except Exception:
                    detail = ""
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt + 1 < self.HTTP_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise NationwideWfsError(
                    "WFS HTTP {0}: {1}".format(exc.code, detail or exc.reason)
                ) from exc
            except URLError as exc:
                if attempt + 1 < self.HTTP_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise NationwideWfsError(
                    "WFS-netwerkfout: " + str(exc.reason)
                ) from exc
            except Exception as exc:
                if attempt + 1 < self.HTTP_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise NationwideWfsError(
                    "WFS-opvraag mislukt: " + str(exc)
                ) from exc

    def _params(
        self,
        type_name,
        label_field,
        count,
        bbox=None,
        start_index=0,
        output_format="application/json",
    ):
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "outputFormat": output_format,
            "srsName": self.algorithm.RD_AUTHID,
            "count": str(count),
            "propertyName": ",".join(
                (label_field, "fid", self.algorithm.GEOMETRY_FIELD)
            ),
            "sortBy": "fid A",
        }
        if start_index:
            params["startIndex"] = str(start_index)
        if bbox is not None:
            _, xmin, ymin, xmax, ymax = bbox
            params["bbox"] = "{0},{1},{2},{3},{4}".format(
                xmin, ymin, xmax, ymax, self.algorithm.RD_AUTHID
            )
        return params

    @staticmethod
    def _gpkg_feature_table(path):
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT table_name FROM gpkg_contents "
                "WHERE data_type='features' ORDER BY table_name LIMIT 1"
            ).fetchone()
            if not row:
                return "", 0
            table_name = row[0]
            safe_name = '"' + str(table_name).replace('"', '""') + '"'
            count = connection.execute(
                "SELECT COUNT(*) FROM " + safe_name
            ).fetchone()[0]
            return str(table_name), int(count)
        finally:
            connection.close()

    def _benchmark_download_format(self, type_name, label_field):
        """Check actual Enexis GeoPackage support and choose the cheaper probe."""
        count = self.FORMAT_BENCHMARK_COUNT
        json_params = self._params(type_name, label_field, count)
        started = time.perf_counter()
        json_data, _ = self._request_raw(
            json_params,
            self.MAX_TILE_PAGE_BYTES,
            "application/json",
        )
        json_seconds = time.perf_counter() - started
        json_size = len(json_data)

        gpkg_params = self._params(
            type_name, label_field, count, output_format="geopkg"
        )
        try:
            started = time.perf_counter()
            gpkg_data, content_type = self._request_raw(
                gpkg_params,
                self.MAX_TILE_PAGE_BYTES,
                "application/geopackage+sqlite3, application/octet-stream",
            )
            gpkg_seconds = time.perf_counter() - started
            supported = (
                gpkg_data.startswith(b"SQLite format 3\x00")
                and (
                    "geopackage" in content_type.lower()
                    or "sqlite" in content_type.lower()
                    or "octet-stream" in content_type.lower()
                )
            )
        except NationwideWfsError as exc:
            self.feedback.pushInfo(
                "Enexis WFS GeoPackage-probe: niet beschikbaar ({0}).".format(exc)
            )
            return "geojson"

        if not supported:
            self.feedback.pushInfo(
                "Enexis WFS GeoPackage-probe: server gaf geen GeoPackage terug; "
                "GeoJSON wordt gebruikt."
            )
            return "geojson"

        gpkg_size = len(gpkg_data)
        self.feedback.pushInfo(
            "Enexis WFS ondersteunt GeoPackage. Probe {0} objecten: GeoJSON "
            "{1:.2f}s/{2:.1f}MB, GeoPackage {3:.2f}s/{4:.1f}MB.".format(
                count,
                json_seconds,
                json_size / 1048576,
                gpkg_seconds,
                gpkg_size / 1048576,
            )
        )

        # GeoPackage has server-side creation overhead. Use it only when the live
        # benchmark shows a meaningful round-trip or transfer-size advantage.
        if gpkg_seconds <= json_seconds * 0.90 or gpkg_size <= json_size * 0.65:
            self.feedback.pushInfo(
                "Landelijke WFS-index kiest GeoPackage als snelste downloadformaat."
            )
            return "geopkg"
        self.feedback.pushInfo(
            "GeoPackage is beschikbaar maar de live probe is niet sneller genoeg; "
            "de tegelindex gebruikt GeoJSON."
        )
        return "geojson"

    def _save_page(self, data, suffix):
        folder = self.cache_folder or os.path.join(
            tempfile.gettempdir(), "enexis_kabel_csv_cache"
        )
        os.makedirs(folder, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="enexis_wfs_tile_", suffix=suffix, dir=folder)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return path

    def _download_tile(self, tile, type_name, label_field, download_format):
        paths = []
        total_returned = 0
        start_index = 0
        page_size = self.TILE_PAGE_SIZE
        try:
            while not self.cancel_event.is_set():
                output_format = "geopkg" if download_format == "geopkg" else "application/json"
                params = self._params(
                    type_name,
                    label_field,
                    page_size,
                    bbox=tile,
                    start_index=start_index,
                    output_format=output_format,
                )
                try:
                    data, _ = self._request_raw(
                        params,
                        self.MAX_TILE_PAGE_BYTES,
                        (
                            "application/geopackage+sqlite3, application/octet-stream"
                            if download_format == "geopkg"
                            else "application/json"
                        ),
                    )
                except NationwideWfsError as exc:
                    text = str(exc).lower()
                    if (
                        ("te groot" in text or "overschrijdt" in text)
                        and page_size > self.MIN_TILE_PAGE_SIZE
                    ):
                        page_size = max(self.MIN_TILE_PAGE_SIZE, page_size // 2)
                        continue
                    raise

                suffix = ".gpkg" if download_format == "geopkg" else ".json"
                path = self._save_page(data, suffix)
                paths.append(path)

                if download_format == "geopkg":
                    _, returned = self._gpkg_feature_table(path)
                else:
                    payload = json.loads(data.decode("utf-8-sig", errors="replace"))
                    returned = len(payload.get("features") or [])

                total_returned += returned
                if returned == 0 or returned < page_size:
                    break
                start_index += returned

            if self.cancel_event.is_set():
                raise NationwideWfsError("Landelijke tegel-download geannuleerd.")
            return tile, paths, total_returned
        except Exception:
            for path in paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _source_key(source_fid, label, geometry_wkb):
        fid = str(source_fid or "").strip()
        if fid:
            return "fid:" + fid
        digest = hashlib.sha1()
        digest.update(str(label).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(geometry_wkb)
        return "geom:" + digest.hexdigest()

    def _records_from_geojson(self, path, label_field):
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        records = []
        for feature in payload.get("features") or []:
            properties = feature.get("properties") or {}
            label = normalize_label(
                self.algorithm._property_value(properties, label_field)
            )
            geometry_json = feature.get("geometry")
            if not label or not geometry_json:
                continue
            geometry = QgsJsonUtils.geometryFromGeoJson(
                json.dumps(geometry_json, separators=(",", ":"))
            )
            if geometry is None or geometry.isEmpty():
                continue
            geometry_wkb = bytes(geometry.asWkb())
            source_fid = properties.get("fid") or feature.get("id") or ""
            source_key = self._source_key(source_fid, label, geometry_wkb)
            records.append(
                (
                    source_key,
                    str(source_fid or source_key),
                    label,
                    round(geometry.length(), 2),
                    sqlite3.Binary(geometry_wkb),
                )
            )
        return records

    def _records_from_gpkg(self, path, label_field):
        layer = QgsVectorLayer(path, "enexis_wfs_tile", "ogr")
        if not layer.isValid():
            raise NationwideWfsError("Gedownloade WFS-GeoPackage kon niet worden geopend.")
        field_lookup = {field.name().casefold(): field.name() for field in layer.fields()}
        actual_label = field_lookup.get(label_field.casefold(), label_field)
        actual_fid = field_lookup.get("fid")
        records = []
        for feature in layer.getFeatures():
            try:
                label_value = feature[actual_label]
            except Exception:
                label_value = None
            label = normalize_label(label_value)
            geometry = feature.geometry()
            if not label or geometry is None or geometry.isEmpty():
                continue
            geometry_wkb = bytes(geometry.asWkb())
            source_fid = ""
            if actual_fid:
                try:
                    source_fid = str(feature[actual_fid] or "")
                except Exception:
                    source_fid = ""
            source_key = self._source_key(source_fid, label, geometry_wkb)
            records.append(
                (
                    source_key,
                    str(source_fid or source_key),
                    label,
                    round(geometry.length(), 2),
                    sqlite3.Binary(geometry_wkb),
                )
            )
        del layer
        return records

    def _build_wfs_index(self, type_name, label_field):
        try:
            builder = WfsIndexBuilder(
                self.cache_folder,
                self.algorithm.WFS_URL,
                type_name,
                label_field,
                self.algorithm.GEOMETRY_FIELD,
                self.algorithm.RD_AUTHID,
            )
        except WfsIndexError as exc:
            raise QgsProcessingException(str(exc)) from exc

        download_format = self._benchmark_download_format(type_name, label_field)
        tiles = tile_bounds(self.NATIONWIDE_RD_BOUNDS, self.TILE_SIZE_M)
        total_tiles = len(tiles)
        completed_tiles = 0
        raw_features = 0
        duplicate_or_invalid = 0
        pending = {}
        tile_iter = iter(tiles)

        self.feedback.pushInfo(
            "Landelijke WFS-index: {0} RD-tegels van {1:.0f} km, maximaal {2} "
            "gelijktijdige downloads, formaat {3}.".format(
                total_tiles,
                self.TILE_SIZE_M / 1000,
                self.MAX_TILE_WORKERS,
                download_format,
            )
        )

        executor = ThreadPoolExecutor(max_workers=self.MAX_TILE_WORKERS)
        try:
            for _ in range(self.MAX_TILE_WORKERS):
                try:
                    tile = next(tile_iter)
                except StopIteration:
                    break
                pending[
                    executor.submit(
                        self._download_tile,
                        tile,
                        type_name,
                        label_field,
                        download_format,
                    )
                ] = tile

            while pending:
                self._cancel_if_requested(
                    "Landelijke WFS-indexbouw geannuleerd; de oude index blijft behouden."
                )
                done, _ = wait(tuple(pending.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    tile = pending.pop(future)
                    _, paths, returned = future.result()
                    raw_features += returned
                    try:
                        for path in paths:
                            records = (
                                self._records_from_gpkg(path, label_field)
                                if download_format == "geopkg"
                                else self._records_from_geojson(path, label_field)
                            )
                            before = builder.connection.execute(
                                "SELECT COUNT(*) FROM wfs_rows"
                            ).fetchone()[0]
                            builder.insert_records(records)
                            after = builder.connection.execute(
                                "SELECT COUNT(*) FROM wfs_rows"
                            ).fetchone()[0]
                            duplicate_or_invalid += max(0, returned - (after - before))
                            del records
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                        builder.commit()
                    finally:
                        for path in paths:
                            try:
                                os.remove(path)
                            except OSError:
                                pass

                    completed_tiles += 1
                    self.feedback.setProgress(
                        5.0 + 45.0 * completed_tiles / max(1, total_tiles)
                    )
                    if completed_tiles % 10 == 0 or completed_tiles == total_tiles:
                        indexed = builder.connection.execute(
                            "SELECT COUNT(*) FROM wfs_rows"
                        ).fetchone()[0]
                        self.feedback.pushInfo(
                            "WFS-index: {0}/{1} tegels, {2} ruwe objecten gezien, "
                            "{3} unieke geldige kabels geïndexeerd.".format(
                                completed_tiles, total_tiles, raw_features, indexed
                            )
                        )

                    try:
                        next_tile = next(tile_iter)
                    except StopIteration:
                        next_tile = None
                    if next_tile is not None:
                        pending[
                            executor.submit(
                                self._download_tile,
                                next_tile,
                                type_name,
                                label_field,
                                download_format,
                            )
                        ] = next_tile

            self._cancel_if_requested(
                "Landelijke WFS-indexbouw geannuleerd; de oude index blijft behouden."
            )
            index = builder.finalize(
                download_format,
                raw_features,
                {
                    "tile_size_m": self.TILE_SIZE_M,
                    "tile_count": total_tiles,
                    "max_workers": self.MAX_TILE_WORKERS,
                },
            )
            return index
        except Exception:
            self.cancel_event.set()
            builder.abort()
            raise
        finally:
            self.cancel_event.set() if self.feedback.isCanceled() else None
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def _open_or_build_wfs_index(self, type_name, label_field):
        if not self.refresh_wfs_index:
            existing = open_existing(
                self.cache_folder,
                self.algorithm.WFS_URL,
                type_name,
                label_field,
                self.algorithm.GEOMETRY_FIELD,
                self.algorithm.RD_AUTHID,
            )
            if existing is not None:
                self.feedback.pushInfo(
                    "Landelijke WFS-index hergebruikt: {0} kabels, {1} labels, "
                    "gebouwd {2}, formaat {3}.".format(
                        existing.meta.get("feature_count", "?"),
                        existing.meta.get("label_count", "?"),
                        existing.meta.get("built_at_utc", "onbekend"),
                        existing.meta.get("download_format", "onbekend"),
                    )
                )
                return existing

        if self.refresh_wfs_index:
            self.feedback.pushInfo(
                "WFS-index vernieuwen is aangevinkt; Enexis wordt opnieuw getegeld gedownload."
            )
        else:
            self.feedback.pushInfo(
                "Geen bruikbare WFS-index gevonden; eenmalige landelijke tegelindex wordt opgebouwd."
            )
        return self._build_wfs_index(type_name, label_field)

    @staticmethod
    def _next_common_labels(connection, last_label, batch_size):
        rows = connection.execute(
            "SELECT w.label FROM wfs.wfs_labels w "
            "WHERE w.label > ? AND EXISTS ("
            "SELECT 1 FROM csv_rows c WHERE c.label=w.label AND c.length_m IS NOT NULL"
            ") ORDER BY w.label LIMIT ?",
            (last_label, batch_size),
        ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _csv_rows_for_labels(connection, labels, fieldnames):
        placeholders = ",".join("?" for _ in labels)
        query = (
            "SELECT row_number, label, length_m, values_json FROM csv_rows "
            "WHERE length_m IS NOT NULL AND label IN ({0}) "
            "ORDER BY label, row_number"
        ).format(placeholders)
        rows = []
        groups = defaultdict(list)
        for row_number, label, length_m, values_json in connection.execute(query, labels):
            values = json.loads(values_json)
            index = len(rows)
            rows.append(
                {
                    "row_number": row_number,
                    "values": dict(zip(fieldnames, values)),
                    "label": label,
                    "length_m": length_m,
                    "length_error": "",
                }
            )
            groups[label].append((index, length_m))
        return rows, groups

    @staticmethod
    def _wfs_records_for_labels(connection, labels):
        placeholders = ",".join("?" for _ in labels)
        query = (
            "SELECT geometry_wkb, label, length_m FROM wfs.wfs_rows "
            "WHERE label IN ({0}) ORDER BY label, source_key"
        ).format(placeholders)
        records = []
        for geometry_wkb, label, length_m in connection.execute(query, labels):
            geometry = QgsGeometry()
            geometry.fromWkb(bytes(geometry_wkb))
            records.append((geometry, label, length_m))
        return records

    def _write_batch(
        self,
        records,
        csv_groups,
        csv_rows,
        output_fields,
        csv_output_names,
        aux_names,
        sink,
    ):
        line_groups = defaultdict(list)
        for index, (_, label, length_m) in enumerate(records):
            if label and length_m is not None:
                line_groups[label].append((index, length_m))

        matches = {}
        matched_csv = set()
        for label in line_groups.keys() & csv_groups.keys():
            for line_idx, csv_idx in optimal_one_to_one(
                line_groups[label], csv_groups[label]
            ):
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)

        output_index = {field.name(): i for i, field in enumerate(output_fields)}
        csv_items = tuple(csv_output_names.items())
        written = 0
        for index, (geometry, label, line_len) in enumerate(records):
            csv_idx = matches.get(index)
            if self.only_matched_output and csv_idx is None:
                continue

            out = QgsFeature(output_fields)
            out.setGeometry(geometry)
            attrs = [None] * len(output_fields)
            attrs[output_index[aux_names["wfs_label_norm"]]] = label
            attrs[output_index[aux_names["wfs_len_m"]]] = line_len

            if csv_idx is None:
                status = (
                    "GEEN_EXACT_LABEL_IN_CSV"
                    if label not in csv_groups
                    else "GEEN_CSV_RIJ_OVER_IN_LABELGROEP"
                )
            else:
                status = "GEKOPPELD"
                row = csv_rows[csv_idx]
                for csv_name, output_name in csv_items:
                    attrs[output_index[output_name]] = row["values"].get(csv_name, "")
                attrs[output_index[aux_names["csv_len_m"]]] = row["length_m"]
                attrs[output_index[aux_names["len_diff_m"]]] = round(
                    abs(line_len - row["length_m"]), 2
                )
                attrs[output_index[aux_names["csv_row_nr"]]] = row["row_number"]

            attrs[output_index[aux_names["match_status"]]] = status
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            written += 1

        matched_row_numbers = [csv_rows[i]["row_number"] for i in matched_csv]
        return written, len(matches), matched_row_numbers

    def _write_unmatched_csv(
        self,
        connection,
        csv_fieldnames,
        unmatched_fields,
        unmatched_sink,
    ):
        total = connection.execute(
            "SELECT COUNT(*) FROM csv_rows c "
            "LEFT JOIN temp.matched_rows m ON m.row_number=c.row_number "
            "WHERE m.row_number IS NULL"
        ).fetchone()[0]
        cursor = connection.execute(
            "SELECT c.row_number, c.label, c.length_m, c.length_error, c.values_json, "
            "CASE WHEN wl.label IS NULL THEN 0 ELSE 1 END "
            "FROM csv_rows c "
            "LEFT JOIN temp.matched_rows m ON m.row_number=c.row_number "
            "LEFT JOIN wfs.wfs_labels wl ON wl.label=c.label "
            "WHERE m.row_number IS NULL ORDER BY c.row_number"
        )
        written = 0
        for row_number, label, length_m, length_error, values_json, wfs_found in cursor:
            self._cancel_if_requested(
                "Schrijven van niet-gekoppelde landelijke CSV-rijen geannuleerd."
            )
            if not label:
                reason = "LEGE_KABEL_SUBGROEP"
            elif length_error:
                reason = length_error
            elif not wfs_found:
                reason = "GEEN_EXACT_LABEL_IN_WFS"
            else:
                reason = "GEEN_WFS_LIJN_OVER_IN_LABELGROEP"
            values = json.loads(values_json)
            values.extend([row_number, label, length_m, reason])
            feature = QgsFeature(unmatched_fields)
            feature.setAttributes(values)
            unmatched_sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)
            written += 1
            if written % 100000 == 0:
                self.feedback.pushInfo(
                    "Niet-gekoppelde landelijke CSV-rijen: {0}/{1}.".format(
                        written, total
                    )
                )
                self.feedback.setProgress(90.0 + 10.0 * written / max(1, total))
        return written

    def run(self):
        self._require_disk_output(
            self.algorithm.OUTPUT, "Gekoppelde Enexis WFS-lijnen"
        )
        if not self.only_matched_output:
            self._require_disk_output(
                self.algorithm.UNMATCHED_CSV, "Niet-gekoppelde CSV-rijen"
            )

        schema_started = time.perf_counter()
        try:
            type_name, label_field = self.algorithm._resolve_type_and_label_field(
                None, self.feedback
            )
        except Exception as exc:
            if isinstance(exc, QgsProcessingException):
                raise
            raise QgsProcessingException(str(exc)) from exc
        schema_seconds = time.perf_counter() - schema_started

        self.feedback.setProgressText("Herbruikbare CSV-index openen of opbouwen...")
        csv_started = time.perf_counter()
        csv_index = self.algorithm._open_csv_index(
            self.csv_path, self.cache_folder, self.feedback
        )
        csv_seconds = time.perf_counter() - csv_started

        wfs_index = None
        try:
            self.feedback.pushInfo(
                "CSV-index {0}: {1} rijen, {2} geldige unieke labels, {3:.1f} MB.".format(
                    "hergebruikt" if csv_index.reused else "nieuw gebouwd",
                    csv_index.stats.get("total_rows", "?"),
                    csv_index.stats.get("valid_unique_labels", "?"),
                    os.path.getsize(csv_index.path) / 1048576,
                )
            )

            wfs_started = time.perf_counter()
            try:
                wfs_index = self._open_or_build_wfs_index(type_name, label_field)
            except NationwideWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc
            wfs_seconds = time.perf_counter() - wfs_started

            connection = csv_index.connection
            connection.execute("ATTACH DATABASE ? AS wfs", (wfs_index.path,))
            connection.execute(
                "CREATE TEMP TABLE matched_rows (row_number INTEGER PRIMARY KEY)"
            )

            csv_fieldnames = list(csv_index.fieldnames)
            output_fields, csv_output_names, aux_names = self.algorithm._output_fields(
                QgsFields(), csv_fieldnames
            )
            sink, dest_id = self.algorithm.parameterAsSink(
                self.parameters,
                self.algorithm.OUTPUT,
                self.context,
                output_fields,
                Qgis.WkbType.MultiLineString,
                self.source_crs,
            )
            if sink is None:
                raise QgsProcessingException(
                    self.algorithm.invalidSinkError(
                        self.parameters, self.algorithm.OUTPUT
                    )
                )

            unmatched_fields = self.algorithm._unmatched_csv_fields(csv_fieldnames)
            unmatched_sink, unmatched_id = self.algorithm.parameterAsSink(
                self.parameters,
                self.algorithm.UNMATCHED_CSV,
                self.context,
                unmatched_fields,
                NO_GEOMETRY,
                self.source_crs,
            )
            if unmatched_sink is None:
                raise QgsProcessingException(
                    self.algorithm.invalidSinkError(
                        self.parameters, self.algorithm.UNMATCHED_CSV
                    )
                )

            total_labels = connection.execute(
                "SELECT COUNT(*) FROM wfs.wfs_labels w WHERE EXISTS ("
                "SELECT 1 FROM csv_rows c WHERE c.label=w.label AND c.length_m IS NOT NULL"
                ")"
            ).fetchone()[0]
            processed_labels = 0
            last_label = ""
            total_written = 0
            total_matches = 0
            matching_started = time.perf_counter()

            while True:
                self._cancel_if_requested(
                    "Landelijke koppeling geannuleerd; gedeeltelijke uitvoer is ongeldig."
                )
                labels = self._next_common_labels(
                    connection, last_label, self.MATCH_LABEL_BATCH
                )
                if not labels:
                    break
                csv_rows, csv_groups = self._csv_rows_for_labels(
                    connection, labels, csv_fieldnames
                )
                records = self._wfs_records_for_labels(connection, labels)
                written, matched, matched_row_numbers = self._write_batch(
                    records,
                    csv_groups,
                    csv_rows,
                    output_fields,
                    csv_output_names,
                    aux_names,
                    sink,
                )
                if matched_row_numbers:
                    connection.executemany(
                        "INSERT OR IGNORE INTO temp.matched_rows(row_number) VALUES (?)",
                        [(row_number,) for row_number in matched_row_numbers],
                    )

                total_written += written
                total_matches += matched
                processed_labels += len(labels)
                last_label = labels[-1]
                if processed_labels % 1000 < len(labels):
                    self.feedback.pushInfo(
                        "Landelijke lokale koppeling: {0}/{1} labels, {2} matches.".format(
                            processed_labels, total_labels, total_matches
                        )
                    )
                self.feedback.setProgress(
                    50.0 + 40.0 * processed_labels / max(1, total_labels)
                )
                del records, csv_rows, csv_groups, matched_row_numbers
                gc.collect()

            matching_seconds = time.perf_counter() - matching_started

            if self.only_matched_output:
                unmatched_count = 0
                self.feedback.pushInfo(
                    "Snelle landelijke uitvoer actief: niet-gekoppelde WFS-kabels en "
                    "CSV-rijen worden niet uitgeschreven."
                )
            else:
                self.feedback.setProgressText(
                    "Niet-gekoppelde landelijke CSV-rijen schrijven..."
                )
                unmatched_count = self._write_unmatched_csv(
                    connection,
                    csv_fieldnames,
                    unmatched_fields,
                    unmatched_sink,
                )

            self.feedback.setProgress(100.0)
            self.feedback.pushInfo(
                "Timing landelijk: schema {0:.2f}s; CSV-index {1:.2f}s; WFS-index "
                "{2:.2f}s; lokale matching {3:.2f}s; totaal {4:.2f}s.".format(
                    schema_seconds,
                    csv_seconds,
                    wfs_seconds,
                    matching_seconds,
                    time.perf_counter() - self.total_started,
                )
            )
            self.feedback.pushInfo(
                "Klaar landelijk: {0} gezamenlijke labels, {1} matches, {2} "
                "WFS-objecten geschreven, {3} niet-gekoppelde CSV-rijen geschreven.".format(
                    processed_labels,
                    total_matches,
                    total_written,
                    unmatched_count,
                )
            )
            return {
                self.algorithm.OUTPUT: dest_id,
                self.algorithm.UNMATCHED_CSV: unmatched_id,
            }
        finally:
            if wfs_index is not None:
                wfs_index.close()
            csv_index.close()
