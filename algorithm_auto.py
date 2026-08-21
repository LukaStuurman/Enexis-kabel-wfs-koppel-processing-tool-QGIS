# -*- coding: utf-8 -*-
"""Two-stage, low-resource Enexis WFS/CSV matcher for QGIS 4.2."""

from __future__ import annotations

import csv
import gc
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections import defaultdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsGeometry,
    QgsJsonUtils,
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)

from .algorithm import FILE_BEHAVIOR, KoppelWfsCsvAlgorithm, NO_GEOMETRY
from .matching import normalize_label, optimal_one_to_one, parse_decimal


class DirectWfsError(RuntimeError):
    pass


class KoppelWfsCsvAutoAlgorithm(KoppelWfsCsvAlgorithm):
    """First scan labels in the extent, then fetch geometry only for CSV matches."""

    WFS_URL = "https://opendata.enexis.nl/geoserver/wfs"
    TYPE_HINT = "e_lv_map_cable"
    GEOMETRY_FIELD = "geografischeligging"
    EXTENT = "EXTENT"
    CACHE_FOLDER = "CACHE_FOLDER"
    RD_EPSG = 28992
    RD_AUTHID = "EPSG:28992"

    # The extent scan contains only one string attribute per feature, so it can
    # safely inspect far more features than the old full-geometry extent query.
    MAX_EXTENT_LABEL_FEATURES = 10000
    MAX_LABEL_SCAN_BYTES = 4 * 1024 * 1024

    # Geometry is requested only for labels which occur in both WFS extent and CSV.
    LABELS_PER_GEOMETRY_REQUEST = 10
    MAX_GEOMETRY_FEATURES_PER_BATCH = 1000
    MAX_GEOMETRY_RESPONSE_BYTES = 8 * 1024 * 1024

    # Nationwide mode downloads the WFS exactly once in stable fid order,
    # keeping one page in memory and all cross-page matching state on disk.
    NATIONWIDE_WFS_PAGE_SIZE = 10000
    MAX_NATIONWIDE_PAGE_BYTES = 64 * 1024 * 1024
    NATIONWIDE_MATCH_LABEL_BATCH = 50

    MAX_METADATA_BYTES = 2 * 1024 * 1024
    HTTP_TIMEOUT_SECONDS = 30
    HTTP_RETRIES = 3
    TYPE_KEY = "enexiskabel/wfs_type_name"
    LABEL_KEY = "enexiskabel/wfs_label_field"

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr(
            "Koppel Enexis WFS-kabels aan CSV (extent of heel Nederland)"
        )

    def shortHelpString(self):
        return self.tr(
            "Met een extent doet de tool eerst een lichte WFS-labelscan. Zonder "
            "extent gebruikt de tool de landelijke modus: de CSV wordt naar een "
            "tijdelijke SQLite-index op schijf gestreamd en daarna per kleine "
            "labelbatch gekoppeld. Daardoor worden niet alle CSV-kolommen en "
            "geometrieën tegelijk in RAM gehouden. Kies voor landelijke uitvoer "
            "GeoPackage-bestanden in plaats van tijdelijke geheugenlagen."
        )

    def createInstance(self):
        return KoppelWfsCsvAutoAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.CSV_FILE,
                self.tr("CSV-bestand"),
                behavior=FILE_BEHAVIOR,
                extension="csv",
            )
        )
        self.addParameter(
            QgsProcessingParameterExtent(
                self.EXTENT,
                self.tr("Beperk WFS tot scherm/gebied (aanbevolen)"),
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT, self.tr("Gekoppelde Enexis WFS-lijnen")
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.UNMATCHED_CSV, self.tr("Niet-gekoppelde CSV-rijen")
            )
        )

    @staticmethod
    def _get_setting(key):
        return str(QSettings().value(key, "") or "").strip()

    @staticmethod
    def _set_setting(key, value):
        settings = QSettings()
        value = str(value or "").strip()
        if value:
            settings.setValue(key, value)
        else:
            settings.remove(key)

    @staticmethod
    def _read_bounded(response, limit):
        raw_size = response.headers.get("Content-Length")
        try:
            size = int(raw_size) if raw_size else 0
        except ValueError:
            size = 0
        if size > limit:
            raise DirectWfsError(
                "WFS-response is te groot ({0:.1f} MB). Zoom verder in of kies een "
                "kleinere extent.".format(size / 1048576)
            )
        data = response.read(limit + 1)
        if len(data) > limit:
            raise DirectWfsError(
                "WFS-response overschrijdt de veilige limiet van {0:.0f} MB. Zoom "
                "verder in of kies een kleinere extent.".format(limit / 1048576)
            )
        return data

    def _request_bytes(self, params, limit):
        request = Request(
            self.WFS_URL + "?" + urlencode(params),
            headers={
                "User-Agent": "QGIS Enexis Kabelkoppeling",
                "Accept": "application/json, application/xml, text/xml",
            },
        )
        for attempt in range(self.HTTP_RETRIES):
            try:
                with urlopen(request, timeout=self.HTTP_TIMEOUT_SECONDS) as response:
                    return self._read_bounded(response, limit)
            except DirectWfsError:
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
                raise DirectWfsError(
                    "WFS HTTP {0}: {1}".format(exc.code, detail or exc.reason)
                ) from exc
            except URLError as exc:
                if attempt + 1 < self.HTTP_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise DirectWfsError(
                    "WFS-netwerkfout: " + str(exc.reason)
                ) from exc
            except Exception as exc:
                if attempt + 1 < self.HTTP_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise DirectWfsError(
                    "WFS-opvraag mislukt: " + str(exc)
                ) from exc

    def _request_json(self, params, limit):
        data = self._request_bytes(params, limit)
        try:
            return json.loads(data.decode("utf-8-sig", errors="replace"))
        except Exception as exc:
            preview = data[:500].decode("utf-8", errors="replace")
            raise DirectWfsError("WFS gaf geen geldige GeoJSON terug: " + preview) from exc

    def _discover_type_name(self, feedback):
        xml_bytes = self._request_bytes(
            {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetCapabilities",
            },
            self.MAX_METADATA_BYTES,
        )
        try:
            root = ElementTree.fromstring(xml_bytes)
        except Exception as exc:
            raise DirectWfsError("GetCapabilities kon niet worden gelezen.") from exc

        names = []
        for feature_type in root.findall(".//{*}FeatureType"):
            node = feature_type.find("{*}Name")
            if node is not None and node.text:
                names.append(node.text.strip())

        needle = self.TYPE_HINT.lower()
        candidates = [name for name in names if needle in name.lower()]
        if not candidates:
            raise DirectWfsError(
                "Geen WFS-featuretype gevonden waarvan de naam 'e_lv_map_cable' bevat."
            )
        exact = [name for name in candidates if name.split(":")[-1].lower() == needle]
        chosen = min(exact or candidates, key=lambda value: (len(value), value))
        self._set_setting(self.TYPE_KEY, chosen)
        feedback.pushInfo("WFS-featuretype opgeslagen: " + chosen)
        return chosen

    @staticmethod
    def _bbox_value(extent):
        return "{0},{1},{2},{3},{4}".format(
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
            KoppelWfsCsvAutoAlgorithm.RD_AUTHID,
        )

    def _getfeature_params(self, type_name, count, extent=None):
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "outputFormat": "application/json",
            "srsName": self.RD_AUTHID,
            "count": str(count),
        }
        if extent is not None:
            params["bbox"] = self._bbox_value(extent)
        return params

    @staticmethod
    def _property_value(properties, field_name):
        if field_name in properties:
            return properties.get(field_name)
        wanted = field_name.lower()
        for key, value in properties.items():
            if str(key).lower() == wanted:
                return value
        return None

    @staticmethod
    def _detect_label_field(properties):
        if not isinstance(properties, dict):
            return None

        keys = [str(key) for key in properties.keys()]
        for key in keys:
            if key.lower() == "label":
                return key
        for key in keys:
            if "label" in key.lower():
                return key
        for key in keys:
            compact = key.lower().replace("_", "").replace("-", "")
            if compact in ("kabelgroep", "kabelgroup", "cablegroup"):
                return key

        # Last-resort content detection. This catches schemas where the field
        # has an unexpected name but stores the known WFS text format.
        for key, value in properties.items():
            text = str(value or "").strip().lower()
            if text.startswith("kabelgroup:"):
                return str(key)
        return None

    def _probe_schema(self, type_name, extent):
        params = self._getfeature_params(type_name, 1, extent)
        payload = self._request_json(params, 512 * 1024)
        features = payload.get("features") or []
        if not features:
            return None, []
        properties = features[0].get("properties") or {}
        return self._detect_label_field(properties), sorted(str(key) for key in properties)

    def _resolve_type_and_label_field(self, extent, feedback):
        type_name = self._get_setting(self.TYPE_KEY) or self.TYPE_HINT
        cached_label = self._get_setting(self.LABEL_KEY) or None

        try:
            detected_label, property_names = self._probe_schema(type_name, extent)
        except DirectWfsError:
            self._set_setting(self.TYPE_KEY, "")
            type_name = self._discover_type_name(feedback)
            detected_label, property_names = self._probe_schema(type_name, extent)

        self._set_setting(self.TYPE_KEY, type_name)
        label_field = detected_label or cached_label
        if detected_label:
            self._set_setting(self.LABEL_KEY, detected_label)
            feedback.pushInfo("WFS-labelveld gedetecteerd: " + detected_label)

        if property_names and not label_field:
            raise QgsProcessingException(
                "Geen WFS-labelveld gevonden. Beschikbare properties: "
                + ", ".join(property_names)
            )
        return type_name, label_field

    def _fetch_extent_labels(self, type_name, label_field, extent):
        if not label_field:
            return set(), 0
        params = self._getfeature_params(
            type_name, self.MAX_EXTENT_LABEL_FEATURES + 1, extent
        )
        # This is the core speed improvement: no geometry and no other attributes.
        params["propertyName"] = label_field
        payload = self._request_json(params, self.MAX_LABEL_SCAN_BYTES)
        features = payload.get("features") or []
        if len(features) > self.MAX_EXTENT_LABEL_FEATURES:
            raise DirectWfsError(
                "Meer dan {0} kabeldelen in de huidige extent. Zoom verder in; de "
                "plugin stopt voordat geometrieën worden opgehaald.".format(
                    self.MAX_EXTENT_LABEL_FEATURES
                )
            )

        labels = set()
        for feature in features:
            properties = feature.get("properties") or {}
            value = self._property_value(properties, label_field)
            label = normalize_label(value)
            if label:
                labels.add(label)
        return labels, len(features)

    @staticmethod
    def _escape_cql(value):
        return str(value).replace("'", "''")

    def _label_filter(self, label_field, labels):
        raw_values = set()
        for label in labels:
            raw_values.add(label)
            raw_values.add("Kabelgroup: " + label)
        quoted = ",".join(
            "'" + self._escape_cql(value) + "'" for value in sorted(raw_values)
        )
        return "{0} IN ({1})".format(label_field, quoted)

    def _extent_label_filter(self, label_field, labels, extent):
        # This GeoServer rejects separate bbox + cql_filter parameters. Put the
        # spatial predicate inside CQL so it can use the spatial index and avoid
        # a nationwide label scan for a tiny canvas extent.
        spatial = "BBOX({0},{1},{2},{3},{4},'{5}')".format(
            self.GEOMETRY_FIELD,
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
            self.RD_AUTHID,
        )
        return spatial + " AND " + self._label_filter(label_field, labels)

    @staticmethod
    def _chunks(values, size):
        values = list(values)
        return [values[pos : pos + size] for pos in range(0, len(values), size)]

    def _fetch_geometry_records(self, type_name, label_field, labels, extent):
        params = self._getfeature_params(
            type_name, self.MAX_GEOMETRY_FEATURES_PER_BATCH + 1
        )
        if extent is None:
            params["cql_filter"] = self._label_filter(label_field, labels)
        else:
            params["cql_filter"] = self._extent_label_filter(
                label_field, labels, extent
            )
        payload = self._request_json(params, self.MAX_GEOMETRY_RESPONSE_BYTES)
        features = payload.get("features") or []
        if len(features) > self.MAX_GEOMETRY_FEATURES_PER_BATCH:
            raise DirectWfsError(
                "Meer dan {0} geometrieën voor de gevonden kabelgroepen. De plugin "
                "stopt om geheugengebruik te begrenzen.".format(
                    self.MAX_GEOMETRY_FEATURES_PER_BATCH
                )
            )

        records = []
        for feature in features:
            properties = feature.get("properties") or {}
            label = normalize_label(self._property_value(properties, label_field))
            geometry_json = feature.get("geometry")
            if not geometry_json:
                geometry = None
            else:
                geometry = QgsJsonUtils.geometryFromGeoJson(
                    json.dumps(geometry_json, separators=(",", ":"))
                )

            # The geometry request is label-based (not bbox-based). Restrict it
            # locally again so a duplicated label elsewhere cannot enter a match.
            if (
                extent is not None
                and geometry is not None
                and not geometry.isEmpty()
                and not geometry.boundingBox().intersects(extent)
            ):
                continue

            length_m = (
                None
                if geometry is None or geometry.isEmpty()
                else round(geometry.length(), 2)
            )
            records.append((geometry, label, length_m))
        return records

    def _build_nationwide_cache(
        self, csv_path, cache_folder, feedback
    ):
        """Stream a large CSV into a compact, disk-backed label index."""
        delimiter, fieldnames = self._csv_schema(csv_path)
        target_folder = (cache_folder or tempfile.gettempdir()).strip()
        os.makedirs(target_folder, exist_ok=True)

        csv_size = os.path.getsize(csv_path)
        free_bytes = shutil.disk_usage(target_folder).free
        # The cache contains the CSV index and the relevant nationwide WFS
        # geometries. Keep a conservative margin for SQLite index creation.
        required_bytes = max(5 * 1024 * 1024 * 1024, csv_size * 8)
        if free_bytes < required_bytes:
            raise QgsProcessingException(
                "Onvoldoende vrije ruimte in de cachemap. Voor deze CSV is "
                "minimaal {0:.1f} GB vrij aanbevolen; beschikbaar is {1:.1f} "
                "GB.".format(required_bytes / 1073741824, free_bytes / 1073741824)
            )

        file_descriptor, cache_path = tempfile.mkstemp(
            prefix="enexis_landelijk_", suffix=".sqlite", dir=target_folder
        )
        os.close(file_descriptor)
        connection = None
        try:
            connection = sqlite3.connect(cache_path)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-16384")
            connection.execute("PRAGMA mmap_size=0")
            connection.execute(
                """
                CREATE TABLE csv_rows (
                    row_number INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    length_m REAL,
                    length_error TEXT NOT NULL,
                    values_json TEXT NOT NULL,
                    matched INTEGER NOT NULL DEFAULT 0,
                    wfs_found INTEGER NOT NULL DEFAULT 0
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
                    if feedback.isCanceled():
                        raise QgsProcessingException("Landelijke koppeling geannuleerd.")

                    total_rows += 1
                    label = normalize_label(row.get(self.CSV_LABEL_FIELD))
                    if not label:
                        empty_labels += 1
                    elif len(sample_labels) < 12:
                        sample_labels.add(label)

                    raw_length = row.get(self.CSV_LENGTH_FIELD)
                    try:
                        length_m = round(parse_decimal(raw_length), 2)
                        length_error = ""
                    except (TypeError, ValueError):
                        length_m = None
                        length_error = "ONGELDIGE_CSV_LENGTE"
                        invalid_lengths += 1

                    values = [(row.get(name) or "") for name in fieldnames]
                    values_json = json.dumps(
                        values, ensure_ascii=False, separators=(",", ":")
                    )
                    pending.append(
                        (
                            row_number,
                            label,
                            length_m,
                            length_error,
                            values_json,
                        )
                    )
                    if len(pending) >= 2000:
                        connection.executemany(insert_sql, pending)
                        pending.clear()

                    if total_rows % 100000 == 0:
                        connection.commit()
                        feedback.pushInfo(
                            "Landelijke CSV-index: {0} rijen naar schijf "
                            "gestreamd...".format(total_rows)
                        )

            if pending:
                connection.executemany(insert_sql, pending)
                pending.clear()
            connection.commit()
            feedback.setProgressText("Landelijke labelindex opbouwen...")
            connection.execute(
                "CREATE INDEX idx_csv_rows_label ON csv_rows(label)"
            )
            connection.execute(
                "CREATE INDEX idx_csv_rows_matched ON csv_rows(matched, row_number)"
            )
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
            return connection, cache_path, fieldnames, stats
        except Exception:
            if connection is not None:
                connection.close()
            try:
                os.remove(cache_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _next_cached_label_batch(connection, last_label, batch_size):
        rows = connection.execute(
            "SELECT DISTINCT label FROM csv_rows "
            "WHERE label <> '' AND length_m IS NOT NULL AND label > ? "
            "ORDER BY label LIMIT ?",
            (last_label, batch_size),
        ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _existing_cached_labels(connection, labels, chunk_size=400):
        labels = sorted(set(label for label in labels if label))
        existing = set()
        for position in range(0, len(labels), chunk_size):
            chunk = labels[position : position + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT DISTINCT label FROM csv_rows WHERE label IN ({0})"
            ).format(placeholders)
            existing.update(
                row[0] for row in connection.execute(query, chunk)
            )
        return existing

    def _fetch_nationwide_geometry_page(
        self, type_name, label_field, start_index, page_size=None
    ):
        page_size = page_size or self.NATIONWIDE_WFS_PAGE_SIZE
        params = self._getfeature_params(
            type_name, page_size
        )
        params["startIndex"] = str(start_index)
        params["sortBy"] = "fid A"
        params["propertyName"] = ",".join(
            (label_field, "fid", self.GEOMETRY_FIELD)
        )
        payload = self._request_json(
            params, self.MAX_NATIONWIDE_PAGE_BYTES
        )
        features = payload.get("features") or []
        raw_total = payload.get("numberMatched")
        try:
            total_matched = int(raw_total)
        except (TypeError, ValueError):
            total_matched = None

        records = []
        for offset, feature in enumerate(features):
            properties = feature.get("properties") or {}
            label = normalize_label(
                self._property_value(properties, label_field)
            )
            source_fid = str(
                properties.get("fid")
                or feature.get("id")
                or "{0}:{1}".format(start_index, offset)
            )
            geometry_json = feature.get("geometry")
            if geometry_json:
                geometry = QgsJsonUtils.geometryFromGeoJson(
                    json.dumps(geometry_json, separators=(",", ":"))
                )
            else:
                geometry = None
            if geometry is None or geometry.isEmpty():
                geometry_wkb = None
                length_m = None
            else:
                geometry_wkb = bytes(geometry.asWkb())
                length_m = round(geometry.length(), 2)
            records.append((source_fid, label, length_m, geometry_wkb))
        return records, len(features), total_matched

    def _cache_nationwide_wfs(
        self, connection, type_name, label_field, feedback
    ):
        connection.execute(
            """
            CREATE TABLE wfs_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_fid TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                length_m REAL,
                geometry_wkb BLOB
            )
            """
        )
        insert_sql = (
            "INSERT OR IGNORE INTO wfs_rows "
            "(source_fid, label, length_m, geometry_wkb) VALUES (?, ?, ?, ?)"
        )
        start_index = 0
        total_matched = None
        downloaded_features = 0
        retained_features = 0
        page_number = 0
        page_size = self.NATIONWIDE_WFS_PAGE_SIZE

        while not feedback.isCanceled():
            page_number += 1
            feedback.setProgressText(
                "Landelijke WFS-geometriepagina {0} ophalen vanaf object "
                "{1}...".format(page_number, start_index)
            )
            try:
                records, returned_count, reported_total = (
                    self._fetch_nationwide_geometry_page(
                        type_name, label_field, start_index, page_size
                    )
                )
            except DirectWfsError as exc:
                message = str(exc).lower()
                size_error = "te groot" in message or "overschrijdt" in message
                if size_error and page_size > 1000:
                    page_size = max(1000, page_size // 2)
                    page_number -= 1
                    feedback.pushInfo(
                        "WFS-pagina was te groot; paginagrootte verlaagd naar "
                        "{0} en dezelfde positie wordt opnieuw geprobeerd.".format(
                            page_size
                        )
                    )
                    continue
                raise
            if reported_total is not None:
                total_matched = reported_total
            if returned_count == 0:
                break

            existing_labels = self._existing_cached_labels(
                connection, (record[1] for record in records)
            )
            retained = [
                (
                    source_fid,
                    label,
                    length_m,
                    sqlite3.Binary(geometry_wkb)
                    if geometry_wkb is not None
                    else None,
                )
                for source_fid, label, length_m, geometry_wkb in records
                if label in existing_labels
            ]
            if retained:
                connection.executemany(insert_sql, retained)
            connection.commit()

            downloaded_features += returned_count
            retained_features += len(retained)
            start_index += returned_count
            feedback.pushInfo(
                "Landelijke WFS: {0}/{1} objecten gedownload; {2} objecten "
                "hebben een label dat ook in de CSV staat.".format(
                    downloaded_features,
                    total_matched if total_matched is not None else "?",
                    retained_features,
                )
            )
            if total_matched:
                feedback.setProgress(
                    15.0
                    + 40.0 * downloaded_features / max(1, total_matched)
                )

            del records, retained, existing_labels
            gc.collect()
            if returned_count < page_size:
                break
            if total_matched is not None and start_index >= total_matched:
                break

        connection.execute(
            "CREATE INDEX idx_wfs_rows_label ON wfs_rows(label)"
        )
        connection.execute(
            "UPDATE csv_rows SET wfs_found=1 WHERE label IN "
            "(SELECT DISTINCT label FROM wfs_rows)"
        )
        connection.commit()
        retained_labels = connection.execute(
            "SELECT COUNT(DISTINCT label) FROM wfs_rows"
        ).fetchone()[0]
        return downloaded_features, retained_features, retained_labels

    @staticmethod
    def _next_cached_wfs_label_batch(connection, last_label, batch_size):
        rows = connection.execute(
            "SELECT DISTINCT label FROM wfs_rows WHERE label > ? "
            "ORDER BY label LIMIT ?",
            (last_label, batch_size),
        ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _cached_wfs_records_for_labels(connection, labels):
        placeholders = ",".join("?" for _ in labels)
        query = (
            "SELECT geometry_wkb, label, length_m FROM wfs_rows "
            "WHERE label IN ({0}) ORDER BY label, id"
        ).format(placeholders)
        records = []
        for geometry_wkb, label, length_m in connection.execute(query, labels):
            if geometry_wkb is None:
                geometry = None
            else:
                geometry = QgsGeometry()
                geometry.fromWkb(bytes(geometry_wkb))
            records.append((geometry, label, length_m))
        return records

    @staticmethod
    def _cached_rows_for_labels(connection, labels, fieldnames):
        placeholders = ",".join("?" for _ in labels)
        query = (
            "SELECT row_number, label, length_m, values_json FROM csv_rows "
            "WHERE length_m IS NOT NULL AND label IN ({0}) "
            "ORDER BY label, row_number"
        ).format(placeholders)
        rows = []
        groups = defaultdict(list)
        for row_number, label, length_m, values_json in connection.execute(
            query, labels
        ):
            values = json.loads(values_json)
            csv_index = len(rows)
            rows.append(
                {
                    "row_number": row_number,
                    "values": dict(zip(fieldnames, values)),
                    "label": label,
                    "length_m": length_m,
                    "length_error": "",
                }
            )
            groups[label].append((csv_index, length_m))
        return rows, groups

    @staticmethod
    def _mark_cached_batch(connection, wfs_labels, matched_row_numbers):
        if wfs_labels:
            connection.executemany(
                "UPDATE csv_rows SET wfs_found=1 WHERE label=?",
                [(label,) for label in wfs_labels],
            )
        if matched_row_numbers:
            connection.executemany(
                "UPDATE csv_rows SET matched=1 WHERE row_number=?",
                [(row_number,) for row_number in matched_row_numbers],
            )

    def _write_cached_unmatched(
        self,
        connection,
        csv_fieldnames,
        unmatched_fields,
        unmatched_sink,
        feedback,
    ):
        total = connection.execute(
            "SELECT COUNT(*) FROM csv_rows WHERE matched=0"
        ).fetchone()[0]
        written = 0
        cursor = connection.execute(
            "SELECT row_number, label, length_m, length_error, values_json, "
            "wfs_found FROM csv_rows WHERE matched=0 ORDER BY row_number"
        )
        for (
            row_number,
            label,
            length_m,
            length_error,
            values_json,
            wfs_found,
        ) in cursor:
            if feedback.isCanceled():
                break
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
                feedback.pushInfo(
                    "Niet-gekoppelde landelijke CSV-rijen geschreven: "
                    "{0}/{1}...".format(written, total)
                )
                feedback.setProgress(90.0 + 10.0 * written / max(1, total))
        return written

    def _write_records(
        self,
        records,
        csv_groups,
        csv_rows,
        output_fields,
        csv_output_names,
        aux_names,
        sink,
        matched_csv,
    ):
        line_groups = defaultdict(list)
        for index, (_, label, length_m) in enumerate(records):
            if label and length_m is not None:
                line_groups[label].append((index, length_m))

        matches = {}
        for label in line_groups.keys() & csv_groups.keys():
            available_csv = [
                item for item in csv_groups[label] if item[0] not in matched_csv
            ]
            for line_idx, csv_idx in optimal_one_to_one(
                line_groups[label], available_csv
            ):
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)

        output_index = {field.name(): i for i, field in enumerate(output_fields)}
        csv_items = tuple(csv_output_names.items())
        for index, (geometry, label, line_len) in enumerate(records):
            out = QgsFeature(output_fields)
            if geometry is not None:
                out.setGeometry(geometry)
            attrs = [None] * len(output_fields)
            csv_idx = matches.get(index)

            attrs[output_index[aux_names["wfs_label_norm"]]] = label
            attrs[output_index[aux_names["wfs_len_m"]]] = line_len

            if not label:
                status = "LEGE_WFS_LABEL"
            elif line_len is None:
                status = "ONGELDIGE_WFS_GEOMETRIE"
            elif label not in csv_groups:
                status = "GEEN_EXACT_LABEL_IN_CSV"
            elif csv_idx is None:
                status = "GEEN_CSV_RIJ_OVER_IN_LABELGROEP"
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
        return len(records), len(matches), set(matches.values())

    def _process_nationwide(
        self,
        parameters,
        context,
        feedback,
        csv_path,
        source_crs,
        cache_folder,
        total_started,
    ):
        """Match a nationwide CSV using a bounded disk-backed working set."""
        feedback.pushInfo(
            "Geen extent gekozen: landelijke schijfmodus wordt gebruikt. "
            "Gebruik GeoPackage-uitvoer in plaats van tijdelijke geheugenlagen."
        )

        schema_started = time.perf_counter()
        try:
            type_name, label_field = self._resolve_type_and_label_field(
                None, feedback
            )
        except DirectWfsError as exc:
            raise QgsProcessingException(str(exc)) from exc
        schema_seconds = time.perf_counter() - schema_started

        connection = None
        cache_path = ""
        try:
            feedback.setProgressText(
                "Landelijke CSV naar tijdelijke schijfindex streamen..."
            )
            csv_started = time.perf_counter()
            connection, cache_path, csv_fieldnames, csv_stats = (
                self._build_nationwide_cache(
                    csv_path, cache_folder, feedback
                )
            )
            csv_seconds = time.perf_counter() - csv_started
            cache_bytes = os.path.getsize(cache_path)
            feedback.pushInfo(
                "Landelijke CSV-index: {0} rijen, {1} geldige unieke labels, "
                "{2} lege labels, {3} ongeldige lengtes; schijfcache {4:.1f} "
                "MB.".format(
                    csv_stats["total_rows"],
                    csv_stats["valid_unique_labels"],
                    csv_stats["empty_labels"],
                    csv_stats["invalid_lengths"],
                    cache_bytes / 1048576,
                )
            )

            output_fields, csv_output_names, aux_names = self._output_fields(
                QgsFields(), csv_fieldnames
            )
            sink, dest_id = self.parameterAsSink(
                parameters,
                self.OUTPUT,
                context,
                output_fields,
                Qgis.WkbType.MultiLineString,
                source_crs,
            )
            if sink is None:
                raise QgsProcessingException(
                    self.invalidSinkError(parameters, self.OUTPUT)
                )

            unmatched_fields = self._unmatched_csv_fields(csv_fieldnames)
            unmatched_sink, unmatched_id = self.parameterAsSink(
                parameters,
                self.UNMATCHED_CSV,
                context,
                unmatched_fields,
                NO_GEOMETRY,
                source_crs,
            )
            if unmatched_sink is None:
                raise QgsProcessingException(
                    self.invalidSinkError(parameters, self.UNMATCHED_CSV)
                )

            feedback.setProgressText(
                "Landelijke WFS één keer paginagewijs naar schijfcache downloaden..."
            )
            wfs_download_started = time.perf_counter()
            try:
                (
                    downloaded_wfs_features,
                    retained_wfs_features,
                    retained_wfs_labels,
                ) = self._cache_nationwide_wfs(
                    connection, type_name, label_field, feedback
                )
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc
            wfs_download_seconds = time.perf_counter() - wfs_download_started
            feedback.pushInfo(
                "Landelijke WFS-schijfcache gereed: {0} objecten gedownload, "
                "{1} objecten met een CSV-label bewaard, verdeeld over {2} "
                "labels. Totale cache: {3:.1f} MB.".format(
                    downloaded_wfs_features,
                    retained_wfs_features,
                    retained_wfs_labels,
                    os.path.getsize(cache_path) / 1048576,
                )
            )

            total_labels = retained_wfs_labels
            processed_labels = 0
            batch_index = 0
            last_label = ""
            total_geometry_records = 0
            total_matches = 0
            found_wfs_sample = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT label FROM wfs_rows ORDER BY label LIMIT 12"
                )
            }
            matching_started = time.perf_counter()

            while not feedback.isCanceled():
                labels = self._next_cached_wfs_label_batch(
                    connection,
                    last_label,
                    self.NATIONWIDE_MATCH_LABEL_BATCH,
                )
                if not labels:
                    break

                batch_index += 1
                feedback.setProgressText(
                    "Landelijke schijfkoppeling: labelbatch {0}, {1}/{2} "
                    "labels verwerkt...".format(
                        batch_index, processed_labels, total_labels
                    )
                )
                csv_rows, csv_groups = self._cached_rows_for_labels(
                    connection, labels, csv_fieldnames
                )
                records = self._cached_wfs_records_for_labels(
                    connection, labels
                )

                matched_local = set()
                written, matched, newly_matched = self._write_records(
                    records,
                    csv_groups,
                    csv_rows,
                    output_fields,
                    csv_output_names,
                    aux_names,
                    sink,
                    matched_local,
                )
                matched_row_numbers = [
                    csv_rows[index]["row_number"]
                    for index in newly_matched
                ]
                self._mark_cached_batch(
                    connection, set(), matched_row_numbers
                )

                total_geometry_records += written
                total_matches += matched
                processed_labels += len(labels)
                last_label = labels[-1]

                if batch_index % 20 == 0:
                    connection.commit()
                    gc.collect()
                    feedback.pushInfo(
                        "Landelijke koppeling: {0}/{1} gezamenlijke labels, "
                        "{2} WFS-geometrieën en {3} koppelingen.".format(
                            processed_labels,
                            total_labels,
                            total_geometry_records,
                            total_matches,
                        )
                    )

                feedback.setProgress(
                    55.0
                    + 35.0 * processed_labels / max(1, total_labels)
                )
                del records, csv_rows, csv_groups, matched_local
            connection.commit()

            matching_seconds = time.perf_counter() - matching_started
            found_wfs_labels = retained_wfs_labels

            if total_matches == 0:
                feedback.pushInfo(
                    "Geen landelijke koppelingen. WFS-voorbeeld: {0}".format(
                        self._sample(found_wfs_sample)
                    )
                )
                feedback.pushInfo(
                    "CSV-voorbeeld: {0}".format(
                        self._sample(csv_stats["sample_labels"])
                    )
                )

            feedback.setProgressText(
                "Niet-gekoppelde CSV-rijen vanaf schijfcache schrijven..."
            )
            unmatched_count = self._write_cached_unmatched(
                connection,
                csv_fieldnames,
                unmatched_fields,
                unmatched_sink,
                feedback,
            )

            feedback.setProgress(100.0)
            feedback.pushInfo(
                "Timing landelijk: schema {0:.2f} s; CSV-schijfindex {1:.2f} "
                "s; WFS-download {2:.2f} s; schijfkoppeling {3:.2f} s; totaal "
                "{4:.2f} s.".format(
                    schema_seconds,
                    csv_seconds,
                    wfs_download_seconds,
                    matching_seconds,
                    time.perf_counter() - total_started,
                )
            )
            feedback.pushInfo(
                "Klaar landelijk: {0} CSV-labels verwerkt, {1} labels in WFS "
                "gevonden, {2} WFS-geometrieën, {3} koppelingen en {4} niet-"
                "gekoppelde CSV-rijen.".format(
                    processed_labels,
                    found_wfs_labels,
                    total_geometry_records,
                    total_matches,
                    unmatched_count,
                )
            )
            return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
        finally:
            if connection is not None:
                connection.close()
            if cache_path:
                try:
                    os.remove(cache_path)
                    feedback.pushInfo("Tijdelijke landelijke schijfcache verwijderd.")
                except OSError as exc:
                    feedback.pushInfo(
                        "Tijdelijke cache kon niet worden verwijderd: " + str(exc)
                    )

    @staticmethod
    def _sample(values, limit=12):
        items = sorted(str(value) for value in values if value)
        return ", ".join(items[:limit]) if items else "(geen)"

    def processAlgorithm(self, parameters, context, feedback):
        total_started = time.perf_counter()
        csv_path = self.parameterAsFile(parameters, self.CSV_FILE, context)
        source_crs = QgsCoordinateReferenceSystem.fromEpsgId(self.RD_EPSG)
        extent = None
        if parameters.get(self.EXTENT) not in (None, ""):
            extent = self.parameterAsExtent(parameters, self.EXTENT, context, source_crs)
            if extent is None or extent.isNull() or extent.isEmpty():
                extent = None

        if extent is None:
            cache_folder = self.parameterAsString(
                parameters, self.CACHE_FOLDER, context
            ).strip()
            if cache_folder.upper() == "TEMPORARY_OUTPUT":
                cache_folder = ""
            return self._process_nationwide(
                parameters,
                context,
                feedback,
                csv_path,
                source_crs,
                cache_folder,
                total_started,
            )

        matched_csv = set()
        found_labels = set()
        total_geometry_records = 0
        total_matches = 0
        type_name = None
        label_field = None
        scanned_count = 0
        schema_seconds = 0.0
        label_scan_seconds = 0.0
        geometry_seconds = 0.0

        # In extent mode WFS labels must come first. They form the tiny allowlist
        # used while streaming a nationwide CSV, preventing millions of complete
        # 22-column row dictionaries from entering memory.
        if extent is not None:
            feedback.setProgressText("WFS-schema en labelveld controleren...")
            try:
                started = time.perf_counter()
                type_name, label_field = self._resolve_type_and_label_field(
                    extent, feedback
                )
                schema_seconds = time.perf_counter() - started

                feedback.setProgressText(
                    "Alleen WFS-labels binnen schermextent ophalen..."
                )
                started = time.perf_counter()
                extent_labels, scanned_count = self._fetch_extent_labels(
                    type_name, label_field, extent
                )
                label_scan_seconds = time.perf_counter() - started
                found_labels.update(extent_labels)
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc

        feedback.setProgressText("CSV één keer streamen en relevante rijen selecteren...")
        csv_started = time.perf_counter()
        csv_allowed_labels = found_labels if extent is not None else None
        csv_fieldnames, csv_rows, csv_stats = self._read_csv(
            csv_path, allowed_labels=csv_allowed_labels
        )
        csv_seconds = time.perf_counter() - csv_started
        if extent is not None:
            feedback.pushInfo(
                "CSV-selectie: {0} rij(en) gelezen, {1} rij(en) voor de {2} "
                "WFS-label(s) in de extent bewaard. Rijen buiten de extent worden "
                "niet als niet-gekoppeld uitgevoerd.".format(
                    csv_stats["total_rows"],
                    csv_stats["retained_rows"],
                    len(found_labels),
                )
            )
        else:
            feedback.pushInfo(
                "Geen extent gekozen: alle {0} CSV-rij(en) zijn ingelezen.".format(
                    csv_stats["total_rows"]
                )
            )

        csv_groups = defaultdict(list)
        csv_pre_unmatched = {}
        for csv_idx, row in enumerate(csv_rows):
            if not row["label"]:
                csv_pre_unmatched[csv_idx] = "LEGE_KABEL_SUBGROEP"
            elif row["length_m"] is None:
                csv_pre_unmatched[csv_idx] = row["length_error"]
            else:
                csv_groups[row["label"]].append((csv_idx, row["length_m"]))

        output_fields, csv_output_names, aux_names = self._output_fields(
            QgsFields(), csv_fieldnames
        )
        sink, dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            output_fields,
            Qgis.WkbType.MultiLineString,
            source_crs,
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        unmatched_fields = self._unmatched_csv_fields(csv_fieldnames)
        unmatched_sink, unmatched_id = self.parameterAsSink(
            parameters,
            self.UNMATCHED_CSV,
            context,
            unmatched_fields,
            NO_GEOMETRY,
            source_crs,
        )
        if unmatched_sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.UNMATCHED_CSV)
            )

        if extent is not None:
            common_labels = sorted(found_labels & set(csv_groups.keys()))
            feedback.pushInfo(
                "Extent-scan: {0} kabeldeel-labels gelezen, {1} unieke labels, "
                "{2} label(s) komen ook in de CSV voor.".format(
                    scanned_count, len(found_labels), len(common_labels)
                )
            )
        elif csv_groups:
            feedback.setProgressText("WFS-schema en labelveld controleren...")
            try:
                started = time.perf_counter()
                type_name, label_field = self._resolve_type_and_label_field(
                    extent, feedback
                )
                schema_seconds = time.perf_counter() - started
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc
            common_labels = sorted(csv_groups.keys())
            found_labels.update(common_labels)
            feedback.pushInfo(
                "Geen extent gekozen: geometrie wordt alleen voor CSV-labels opgehaald."
            )
        else:
            common_labels = []

        if not common_labels:
            feedback.pushInfo(
                "Geen exacte labelovereenkomst. WFS-voorbeeld: {0}".format(
                    self._sample(found_labels)
                )
            )
            csv_example = csv_groups.keys() or csv_stats["sample_labels"]
            feedback.pushInfo(
                "CSV-voorbeeld: {0}".format(self._sample(csv_example))
            )
        else:
            batches = self._chunks(
                common_labels, self.LABELS_PER_GEOMETRY_REQUEST
            )
            geometry_started = time.perf_counter()
            try:
                for batch_index, labels in enumerate(batches):
                    if feedback.isCanceled():
                        break
                    feedback.setProgressText(
                        "Geometrie voor matches ophalen {0}/{1}...".format(
                            batch_index + 1, len(batches)
                        )
                    )
                    records = self._fetch_geometry_records(
                        type_name, label_field, labels, extent
                    )
                    written, matched, _ = self._write_records(
                        records,
                        csv_groups,
                        csv_rows,
                        output_fields,
                        csv_output_names,
                        aux_names,
                        sink,
                        matched_csv,
                    )
                    total_geometry_records += written
                    total_matches += matched
                    del records
                    gc.collect()
                    feedback.setProgress(
                        80.0 * (batch_index + 1) / max(1, len(batches))
                    )
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc
            geometry_seconds = time.perf_counter() - geometry_started

        unmatched_indices = [
            index for index in range(len(csv_rows)) if index not in matched_csv
        ]
        for csv_idx in unmatched_indices:
            row = csv_rows[csv_idx]
            if csv_idx in csv_pre_unmatched:
                reason = csv_pre_unmatched[csv_idx]
            elif row["label"] not in found_labels and extent is None:
                reason = "GEEN_EXACT_LABEL_IN_WFS"
            else:
                reason = "GEEN_WFS_LIJN_OVER_IN_LABELGROEP"

            feature = QgsFeature(unmatched_fields)
            values = [row["values"].get(name, "") for name in csv_fieldnames]
            values.extend([row["row_number"], row["label"], row["length_m"], reason])
            feature.setAttributes(values)
            unmatched_sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)

        feedback.setProgress(100.0)
        feedback.pushInfo(
            "Timing: schema {0:.2f} s; labelscan {1:.2f} s; CSV {2:.2f} s; "
            "WFS-geometrie {3:.2f} s; totaal {4:.2f} s.".format(
                schema_seconds,
                label_scan_seconds,
                csv_seconds,
                geometry_seconds,
                time.perf_counter() - total_started,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.CACHE_FOLDER,
                self.tr(
                    "Landelijke modus: tijdelijke cachemap op lokale SSD (optioneel)"
                ),
                optional=True,
            )
        )
        feedback.pushInfo(
            "Klaar: {0} relevante WFS-geometrie(ën) verwerkt, {1} koppeling(en), "
            "{2} CSV-rij(en) zonder match.".format(
                total_geometry_records, total_matches, len(unmatched_indices)
            )
        )
        return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
