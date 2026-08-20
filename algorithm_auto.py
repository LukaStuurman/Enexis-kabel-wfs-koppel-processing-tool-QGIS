# -*- coding: utf-8 -*-
"""Fast and crash-safe automatic Enexis WFS/CSV matching algorithm."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsJsonUtils,
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsWkbTypes,
)

from .algorithm import FILE_BEHAVIOR, KoppelWfsCsvAlgorithm, NO_GEOMETRY
from .matching import normalize_label, optimal_one_to_one


class DirectWfsError(RuntimeError):
    pass


class KoppelWfsCsvAutoAlgorithm(KoppelWfsCsvAlgorithm):
    """Fetch filtered Enexis cables directly as GeoJSON and match them to CSV."""

    WFS_URL = "https://opendata.enexis.nl/geoserver/wfs"
    TYPE_NAME_CONTAINS = "e_lv_map_cable"
    LABEL_FIELD = "label"
    EXTENT = "EXTENT"
    RD_AUTHID = "EPSG:28992"
    LABELS_PER_REQUEST = 40
    MAX_HTTP_WORKERS = 4
    SETTINGS_TYPE_KEY = "enexiskabel/wfs_type_name"
    _cached_type_name = None

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr("Koppel Enexis WFS-kabels automatisch aan CSV (1-op-1)")

    def shortHelpString(self):
        return self.tr(
            "Je kiest de CSV en optioneel een extent. De plugin gebruikt geen live "
            "QGIS-WFS-laag meer, maar stuurt rechtstreeks een gefilterd GetFeature-"
            "verzoek naar Enexis en leest alleen het GeoJSON-resultaat in. Hierdoor "
            "worden de schermextent en Kabel Subgroep al op de server toegepast."
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
                self.tr(
                    "Beperk WFS tot scherm/gebied (optioneel; kies huidige kaartcanvas)"
                ),
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Gekoppelde Enexis WFS-lijnen"),
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.UNMATCHED_CSV,
                self.tr("Niet-gekoppelde CSV-rijen"),
            )
        )

    @staticmethod
    def _feature_type_names(xml_bytes):
        root = ElementTree.fromstring(xml_bytes)
        names = []
        for feature_type in root.findall(".//{*}FeatureType"):
            name_node = feature_type.find("{*}Name")
            if name_node is not None and name_node.text:
                names.append(name_node.text.strip())
        return names

    @classmethod
    def _stored_type_name(cls):
        if cls._cached_type_name:
            return cls._cached_type_name
        value = str(QSettings().value(cls.SETTINGS_TYPE_KEY, "") or "").strip()
        if value:
            cls._cached_type_name = value
        return value

    @classmethod
    def _store_type_name(cls, value):
        value = str(value or "").strip()
        cls._cached_type_name = value or None
        settings = QSettings()
        if value:
            settings.setValue(cls.SETTINGS_TYPE_KEY, value)
        else:
            settings.remove(cls.SETTINGS_TYPE_KEY)

    def _discover_type_name(self, feedback):
        cached = self._stored_type_name()
        if cached:
            feedback.pushInfo("WFS-laagnaam uit permanente cache: " + cached)
            return cached

        capabilities_url = self.WFS_URL + "?" + urlencode(
            {
                "service": "WFS",
                "request": "GetCapabilities",
                "version": "2.0.0",
            }
        )
        request = Request(
            capabilities_url,
            headers={"User-Agent": "QGIS Enexis Kabelkoppeling"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                xml_bytes = response.read()
        except Exception as exc:
            raise DirectWfsError(
                "Kon Enexis WFS GetCapabilities niet ophalen: " + str(exc)
            ) from exc

        try:
            all_names = self._feature_type_names(xml_bytes)
        except Exception as exc:
            raise DirectWfsError(
                "Kon Enexis WFS GetCapabilities niet lezen: " + str(exc)
            ) from exc

        needle = self.TYPE_NAME_CONTAINS.lower()
        candidates = [name for name in all_names if needle in name.lower()]
        if not candidates:
            raise DirectWfsError(
                "Geen Enexis WFS-laag gevonden waarvan de naam 'e_lv_map_cable' bevat."
            )

        exact_local = [
            name for name in candidates if name.split(":")[-1].lower() == needle
        ]
        chosen = min(exact_local or candidates, key=lambda value: (len(value), value))
        self._store_type_name(chosen)
        feedback.pushInfo("Automatisch gevonden WFS-laag: " + chosen)
        return chosen

    @staticmethod
    def _escape_cql_string(value):
        return str(value).replace("'", "''")

    def _cql_for_labels(self, labels):
        raw_values = set()
        for label in labels:
            raw_values.add(label)
            raw_values.add("Kabelgroup: " + label)
        values = ",".join(
            "'" + self._escape_cql_string(value) + "'"
            for value in sorted(raw_values)
        )
        return f"{self.LABEL_FIELD} IN ({values})"

    def _build_getfeature_url(self, type_name, labels, extent):
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "outputFormat": "application/json",
            "srsName": self.RD_AUTHID,
            "cql_filter": self._cql_for_labels(labels),
        }
        if extent is not None:
            params["bbox"] = "{0},{1},{2},{3},{4}".format(
                extent.xMinimum(),
                extent.yMinimum(),
                extent.xMaximum(),
                extent.yMaximum(),
                self.RD_AUTHID,
            )
        return self.WFS_URL + "?" + urlencode(params)

    @staticmethod
    def _http_get_geojson(url):
        request = Request(
            url,
            headers={
                "User-Agent": "QGIS Enexis Kabelkoppeling",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                text = response.read().decode(charset, errors="replace")
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise DirectWfsError(
                f"WFS GetFeature HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except URLError as exc:
            raise DirectWfsError("WFS GetFeature netwerkfout: " + str(exc.reason)) from exc
        except Exception as exc:
            raise DirectWfsError("WFS GetFeature mislukt: " + str(exc)) from exc

        if not text.lstrip().startswith("{"):
            raise DirectWfsError(
                "WFS gaf geen GeoJSON terug: " + text.strip()[:500]
            )
        return text

    @staticmethod
    def _chunks(values, size):
        return [values[pos : pos + size] for pos in range(0, len(values), size)]

    def _download_geojson_batches(self, type_name, labels, extent, feedback):
        batches = self._chunks(sorted(labels), self.LABELS_PER_REQUEST)
        if not batches:
            return []

        urls = [self._build_getfeature_url(type_name, batch, extent) for batch in batches]
        if len(urls) == 1:
            feedback.pushInfo("1 directe gefilterde WFS GetFeature-oproep uitvoeren...")
            return [self._http_get_geojson(urls[0])]

        workers = min(self.MAX_HTTP_WORKERS, len(urls))
        feedback.pushInfo(
            f"{len(urls)} directe WFS-oproepen uitvoeren met maximaal {workers} HTTP-threads..."
        )
        results = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(self._http_get_geojson, url): index
                for index, url in enumerate(urls)
            }
            completed = 0
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                results[index] = future.result()
                completed += 1
                feedback.setProgress(min(45.0, completed * 45.0 / len(urls)))
        return results

    def _fetch_with_type_fallback(self, labels, extent, feedback):
        cached = self._stored_type_name()
        first_type = cached or self.TYPE_NAME_CONTAINS
        try:
            texts = self._download_geojson_batches(first_type, labels, extent, feedback)
            self._store_type_name(first_type)
            if cached:
                feedback.pushInfo("WFS-laagnaamcache werkte zonder GetCapabilities.")
            else:
                feedback.pushInfo(
                    "Ongekwalificeerde laagnaam werkte direct; GetCapabilities overgeslagen."
                )
            return first_type, texts
        except DirectWfsError as first_error:
            if cached:
                feedback.pushInfo(
                    "Opgeslagen WFS-laagnaam werkte niet meer; opnieuw detecteren..."
                )
                self._store_type_name("")
            else:
                feedback.pushInfo(
                    "Directe laagnaam werkte niet; éénmalig volledige typename zoeken..."
                )

            type_name = self._discover_type_name(feedback)
            if type_name == first_type:
                raise first_error
            texts = self._download_geojson_batches(type_name, labels, extent, feedback)
            return type_name, texts

    @staticmethod
    def _detect_label_field(fields):
        names = [field.name() for field in fields]
        for name in names:
            if name.lower() == "label":
                return name
        for name in names:
            if "label" in name.lower():
                return name
        return None

    @staticmethod
    def _parse_geojson_batches(texts):
        if not texts:
            return QgsFields(), []

        source_fields = QgsFields()
        for text in texts:
            candidate = QgsJsonUtils.stringToFields(text)
            if len(candidate):
                source_fields = candidate
                break

        features = []
        for text in texts:
            features.extend(QgsJsonUtils.stringToFeatureList(text, source_fields))
        return source_fields, features

    def processAlgorithm(self, parameters, context, feedback):
        csv_path = self.parameterAsFile(parameters, self.CSV_FILE, context)
        csv_fieldnames, csv_rows = self._read_csv(csv_path)

        csv_groups = defaultdict(list)
        csv_pre_unmatched = {}
        for csv_idx, row in enumerate(csv_rows):
            if not row["label"]:
                csv_pre_unmatched[csv_idx] = "LEGE_KABEL_SUBGROEP"
            elif row["length_m"] is None:
                csv_pre_unmatched[csv_idx] = row["length_error"]
            else:
                csv_groups[row["label"]].append((csv_idx, row["length_m"]))

        source_crs = QgsCoordinateReferenceSystem(self.RD_AUTHID)
        extent = None
        extent_value = parameters.get(self.EXTENT)
        if extent_value not in (None, ""):
            extent = self.parameterAsExtent(
                parameters,
                self.EXTENT,
                context,
                source_crs,
            )
            if extent is None or extent.isNull() or extent.isEmpty():
                extent = None
            else:
                feedback.pushInfo("Scherm/gebied-extent wordt direct als WFS BBOX verstuurd.")

        type_name = self._stored_type_name() or self.TYPE_NAME_CONTAINS
        geojson_texts = []
        source_fields = QgsFields()
        source_features = []

        if csv_groups:
            feedback.setProgressText("Direct gefilterde Enexis WFS-data ophalen...")
            try:
                type_name, geojson_texts = self._fetch_with_type_fallback(
                    csv_groups.keys(), extent, feedback
                )
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc
            source_fields, source_features = self._parse_geojson_batches(geojson_texts)
        else:
            feedback.pushInfo(
                "CSV bevat geen geldige Kabel Subgroep + lengte-combinaties; "
                "er wordt geen WFS-oproep gedaan."
            )

        label_field = self._detect_label_field(source_fields)
        if source_features and not label_field:
            raise QgsProcessingException(
                "WFS GeoJSON bevat features maar geen veld met 'label' in de naam."
            )

        source_wkb_type = QgsWkbTypes.LineString
        for feature in source_features:
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                source_wkb_type = geometry.wkbType()
                break

        output_fields, csv_output_names, aux_names = self._output_fields(
            source_fields,
            csv_fieldnames,
        )
        sink, dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            output_fields,
            source_wkb_type,
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

        line_records = []
        line_groups = defaultdict(list)
        for feature in source_features:
            if feedback.isCanceled():
                break
            label = normalize_label(feature[label_field]) if label_field else ""
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                length_m = None
            else:
                length_m = round(geometry.length(), 2)

            line_idx = len(line_records)
            line_records.append((feature, label, length_m))
            if label and length_m is not None:
                line_groups[label].append((line_idx, length_m))

        feedback.setProgress(60.0)
        matches = {}
        matched_csv = set()
        for label in line_groups.keys() & csv_groups.keys():
            if feedback.isCanceled():
                break
            for line_idx, csv_idx in optimal_one_to_one(
                line_groups[label], csv_groups[label]
            ):
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)

        feedback.setProgress(72.0)
        output_index = {field.name(): i for i, field in enumerate(output_fields)}
        source_field_count = len(source_fields)
        csv_output_items = tuple(csv_output_names.items())

        for idx, (source_feature, label, line_len) in enumerate(line_records):
            if feedback.isCanceled():
                break

            out = QgsFeature(output_fields)
            out.setGeometry(source_feature.geometry())
            attrs = list(source_feature.attributes()) + [None] * (
                len(output_fields) - source_field_count
            )
            csv_idx = matches.get(idx)

            attrs[output_index[aux_names["wfs_label_norm"]]] = label
            attrs[output_index[aux_names["wfs_len_m"]]] = line_len

            if not label:
                status = "LEGE_WFS_LABEL"
            elif line_len is None:
                status = "ONGELDIGE_WFS_GEOMETRIE"
            elif csv_idx is None and label not in csv_groups:
                status = "GEEN_EXACT_LABEL_IN_CSV"
            elif csv_idx is None:
                status = "GEEN_CSV_RIJ_OVER_IN_LABELGROEP"
            else:
                status = "GEKOPPELD"
                row = csv_rows[csv_idx]
                row_values = row["values"]
                for csv_name, output_name in csv_output_items:
                    attrs[output_index[output_name]] = row_values.get(csv_name, "")
                attrs[output_index[aux_names["csv_len_m"]]] = row["length_m"]
                attrs[output_index[aux_names["len_diff_m"]]] = round(
                    abs(line_len - row["length_m"]), 2
                )
                attrs[output_index[aux_names["csv_row_nr"]]] = row["row_number"]

            attrs[output_index[aux_names["match_status"]]] = status
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.FastInsert)

        feedback.setProgress(90.0)
        unmatched_indices = [
            idx for idx in range(len(csv_rows)) if idx not in matched_csv
        ]
        for csv_idx in unmatched_indices:
            row = csv_rows[csv_idx]
            if csv_idx in csv_pre_unmatched:
                reason = csv_pre_unmatched[csv_idx]
            elif row["label"] not in line_groups:
                reason = (
                    "GEEN_MATCH_BINNEN_EXTENT"
                    if extent is not None
                    else "GEEN_EXACT_LABEL_IN_WFS"
                )
            else:
                reason = "GEEN_WFS_LIJN_OVER_IN_LABELGROEP"

            feature = QgsFeature(unmatched_fields)
            values = [row["values"].get(name, "") for name in csv_fieldnames]
            values.extend(
                [row["row_number"], row["label"], row["length_m"], reason]
            )
            feature.setAttributes(values)
            unmatched_sink.addFeature(feature, QgsFeatureSink.FastInsert)

        feedback.setProgress(100.0)
        feedback.pushInfo(
            "Klaar via directe GeoJSON WFS-oproep ({0}): {1} koppeling(en), "
            "{2} WFS-lijn(en) zonder CSV-match en {3} CSV-rij(en) zonder WFS-match.".format(
                type_name,
                len(matches),
                len(line_records) - len(matches),
                len(unmatched_indices),
            )
        )
        return {
            self.OUTPUT: dest_id,
            self.UNMATCHED_CSV: unmatched_id,
        }
