# -*- coding: utf-8 -*-
"""Low-resource Enexis WFS/CSV matching algorithm for QGIS 4.2."""

from __future__ import annotations

import gc
from collections import defaultdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from qgis.PyQt.QtCore import QMetaType, QSettings
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsJsonUtils,
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
)

from .algorithm import FILE_BEHAVIOR, KoppelWfsCsvAlgorithm, NO_GEOMETRY
from .matching import normalize_label, optimal_one_to_one


class DirectWfsError(RuntimeError):
    pass


class KoppelWfsCsvAutoAlgorithm(KoppelWfsCsvAlgorithm):
    WFS_URL = "https://opendata.enexis.nl/geoserver/wfs"
    TYPE_NAME_CONTAINS = "e_lv_map_cable"
    LABEL_FIELD = "label"
    EXTENT = "EXTENT"
    RD_EPSG = 28992
    RD_AUTHID = "EPSG:28992"

    # Hard safety limits. The plugin must fail safely instead of exhausting RAM.
    LABELS_PER_REQUEST = 5
    MAX_FEATURES_PER_BATCH = 500
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_CAPABILITIES_BYTES = 2 * 1024 * 1024
    HTTP_TIMEOUT_SECONDS = 30
    MAX_LABELS_WITHOUT_EXTENT = 20

    SETTINGS_TYPE_KEY = "enexiskabel/wfs_type_name"
    _cached_type_name = None

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr("Koppel Enexis WFS-kabels automatisch aan CSV (low-resource)")

    def shortHelpString(self):
        return self.tr(
            "QGIS 4.2 low-resource variant. Er is maximaal één WFS-request tegelijk. "
            "Kabelgroepen worden in kleine batches opgehaald, direct gekoppeld en daarna "
            "uit het geheugen verwijderd. Gebruik bij voorkeur de huidige kaartcanvas-"
            "extent. Bij een te grote response stopt de tool met een foutmelding in plaats "
            "van extra geheugen te blijven gebruiken."
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
                self.tr("Beperk WFS tot scherm/gebied (sterk aanbevolen)"),
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

    @staticmethod
    def _feature_type_names(xml_bytes):
        root = ElementTree.fromstring(xml_bytes)
        names = []
        for feature_type in root.findall(".//{*}FeatureType"):
            name_node = feature_type.find("{*}Name")
            if name_node is not None and name_node.text:
                names.append(name_node.text.strip())
        return names

    @staticmethod
    def _read_bounded(response, limit):
        value = response.headers.get("Content-Length")
        if value:
            try:
                size = int(value)
            except ValueError:
                size = 0
            if size > limit:
                raise DirectWfsError(
                    "WFS-response is te groot ({0:.1f} MB). Zoom verder in of kies "
                    "een kleinere extent.".format(size / 1048576)
                )

        data = response.read(limit + 1)
        if len(data) > limit:
            raise DirectWfsError(
                "WFS-response is groter dan de veilige limiet van {0:.0f} MB. "
                "Zoom verder in of kies een kleinere extent.".format(limit / 1048576)
            )
        return data

    def _discover_type_name(self, feedback):
        url = self.WFS_URL + "?" + urlencode(
            {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetCapabilities",
            }
        )
        request = Request(url, headers={"User-Agent": "QGIS Enexis Kabelkoppeling"})
        try:
            with urlopen(request, timeout=self.HTTP_TIMEOUT_SECONDS) as response:
                xml_bytes = self._read_bounded(
                    response, self.MAX_CAPABILITIES_BYTES
                )
            all_names = self._feature_type_names(xml_bytes)
        except DirectWfsError:
            raise
        except Exception as exc:
            raise DirectWfsError(
                "Kon de Enexis WFS-laagnaam niet bepalen: " + str(exc)
            ) from exc

        needle = self.TYPE_NAME_CONTAINS.lower()
        candidates = [name for name in all_names if needle in name.lower()]
        if not candidates:
            raise DirectWfsError(
                "Geen WFS-laag gevonden waarvan de naam 'e_lv_map_cable' bevat."
            )
        exact = [name for name in candidates if name.split(":")[-1].lower() == needle]
        chosen = min(exact or candidates, key=lambda value: (len(value), value))
        self._store_type_name(chosen)
        feedback.pushInfo("WFS-laagnaam éénmalig gevonden en opgeslagen: " + chosen)
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
            "count": str(self.MAX_FEATURES_PER_BATCH + 1),
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

    def _http_get_geojson(self, url):
        request = Request(
            url,
            headers={
                "User-Agent": "QGIS Enexis Kabelkoppeling",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.HTTP_TIMEOUT_SECONDS) as response:
                data = self._read_bounded(response, self.MAX_RESPONSE_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
        except DirectWfsError:
            raise
        except HTTPError as exc:
            try:
                detail = exc.read(1000).decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise DirectWfsError(
                f"WFS GetFeature HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except URLError as exc:
            raise DirectWfsError("WFS-netwerkfout: " + str(exc.reason)) from exc
        except Exception as exc:
            raise DirectWfsError("WFS GetFeature mislukt: " + str(exc)) from exc

        text = data.decode(charset, errors="replace")
        if not text.lstrip().startswith("{"):
            raise DirectWfsError(
                "WFS gaf geen GeoJSON terug: " + text.strip()[:500]
            )
        return text

    def _parse_minimal_features(self, text):
        fields = QgsFields()
        fields.append(QgsField(self.LABEL_FIELD, QMetaType.Type.QString))
        features = QgsJsonUtils.stringToFeatureList(text, fields)
        if len(features) > self.MAX_FEATURES_PER_BATCH:
            raise DirectWfsError(
                "Meer dan {0} kabels in één WFS-batch. Zoom verder in of kies "
                "een kleinere extent.".format(self.MAX_FEATURES_PER_BATCH)
            )
        return features

    def _download_batch(self, type_name, labels, extent):
        text = self._http_get_geojson(
            self._build_getfeature_url(type_name, labels, extent)
        )
        try:
            return self._parse_minimal_features(text)
        finally:
            del text

    def _download_first_batch(self, labels, extent, feedback):
        cached = self._stored_type_name()
        type_name = cached or self.TYPE_NAME_CONTAINS
        try:
            features = self._download_batch(type_name, labels, extent)
            self._store_type_name(type_name)
            return type_name, features
        except DirectWfsError as first_error:
            self._store_type_name("")
            if cached:
                feedback.pushInfo("Opgeslagen WFS-laagnaam werkt niet; opnieuw zoeken.")
            else:
                feedback.pushInfo(
                    "Directe laagnaam werkt niet; volledige typename éénmalig zoeken."
                )
            discovered = self._discover_type_name(feedback)
            if discovered == type_name:
                raise first_error
            return discovered, self._download_batch(discovered, labels, extent)

    @staticmethod
    def _chunks(values, size):
        values = list(values)
        return [values[pos : pos + size] for pos in range(0, len(values), size)]

    @staticmethod
    def _first_wkb_type(features):
        for feature in features:
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                return geometry.wkbType()
        return Qgis.WkbType.LineString

    def _process_batch(
        self,
        features,
        csv_groups,
        csv_rows,
        output_fields,
        csv_output_names,
        aux_names,
        sink,
        matched_csv,
        found_labels,
        feedback,
    ):
        records = []
        line_groups = defaultdict(list)
        for feature in features:
            if feedback.isCanceled():
                break
            label = normalize_label(feature[self.LABEL_FIELD])
            geometry = feature.geometry()
            length_m = None if geometry is None or geometry.isEmpty() else round(
                geometry.length(), 2
            )
            index = len(records)
            records.append((geometry, label, length_m))
            if label:
                found_labels.add(label)
            if label and length_m is not None:
                line_groups[label].append((index, length_m))

        matches = {}
        for label in line_groups.keys() & csv_groups.keys():
            for line_idx, csv_idx in optimal_one_to_one(
                line_groups[label], csv_groups[label]
            ):
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)

        output_index = {field.name(): i for i, field in enumerate(output_fields)}
        csv_output_items = tuple(csv_output_names.items())
        for idx, (geometry, label, line_len) in enumerate(records):
            out = QgsFeature(output_fields)
            out.setGeometry(geometry)
            attrs = [None] * len(output_fields)
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
                for csv_name, output_name in csv_output_items:
                    attrs[output_index[output_name]] = row["values"].get(csv_name, "")
                attrs[output_index[aux_names["csv_len_m"]]] = row["length_m"]
                attrs[output_index[aux_names["len_diff_m"]]] = round(
                    abs(line_len - row["length_m"]), 2
                )
                attrs[output_index[aux_names["csv_row_nr"]]] = row["row_number"]

            attrs[output_index[aux_names["match_status"]]] = status
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)

        return len(records), len(matches)

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

        source_crs = QgsCoordinateReferenceSystem.fromEpsgId(self.RD_EPSG)
        extent = None
        if parameters.get(self.EXTENT) not in (None, ""):
            extent = self.parameterAsExtent(
                parameters, self.EXTENT, context, source_crs
            )
            if extent is None or extent.isNull() or extent.isEmpty():
                extent = None
            else:
                feedback.pushInfo("Schermextent wordt direct als WFS BBOX verstuurd.")

        if extent is None and len(csv_groups) > self.MAX_LABELS_WITHOUT_EXTENT:
            raise QgsProcessingException(
                "Veiligheidsstop: {0} geldige kabelgroepen zonder extent. Kies de "
                "huidige kaartcanvas-extent of een kleiner gebied.".format(len(csv_groups))
            )

        output_fields, csv_output_names, aux_names = self._output_fields(
            QgsFields(), csv_fieldnames
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

        sink = None
        dest_id = None
        type_name = self._stored_type_name() or self.TYPE_NAME_CONTAINS
        matched_csv = set()
        found_labels = set()
        total_wfs = 0
        total_matches = 0

        batches = self._chunks(sorted(csv_groups.keys()), self.LABELS_PER_REQUEST)
        feedback.pushInfo(
            "Low-resource modus: {0} kabelgroep(en), {1} WFS-batch(es), "
            "maximaal één request tegelijk.".format(len(csv_groups), len(batches))
        )

        for batch_index, labels in enumerate(batches):
            if feedback.isCanceled():
                break
            feedback.setProgressText(
                "WFS-batch {0}/{1} ophalen...".format(batch_index + 1, len(batches))
            )
            try:
                if batch_index == 0:
                    type_name, features = self._download_first_batch(
                        labels, extent, feedback
                    )
                else:
                    features = self._download_batch(type_name, labels, extent)
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc

            if sink is None and features:
                sink, dest_id = self.parameterAsSink(
                    parameters,
                    self.OUTPUT,
                    context,
                    output_fields,
                    self._first_wkb_type(features),
                    source_crs,
                )
                if sink is None:
                    raise QgsProcessingException(
                        self.invalidSinkError(parameters, self.OUTPUT)
                    )

            if sink is not None and features:
                processed, matched = self._process_batch(
                    features,
                    csv_groups,
                    csv_rows,
                    output_fields,
                    csv_output_names,
                    aux_names,
                    sink,
                    matched_csv,
                    found_labels,
                    feedback,
                )
                total_wfs += processed
                total_matches += matched

            del features
            gc.collect()
            feedback.setProgress(80.0 * (batch_index + 1) / max(1, len(batches)))

        if sink is None:
            sink, dest_id = self.parameterAsSink(
                parameters,
                self.OUTPUT,
                context,
                output_fields,
                Qgis.WkbType.LineString,
                source_crs,
            )
            if sink is None:
                raise QgsProcessingException(
                    self.invalidSinkError(parameters, self.OUTPUT)
                )

        unmatched_indices = [
            index for index in range(len(csv_rows)) if index not in matched_csv
        ]
        for csv_idx in unmatched_indices:
            row = csv_rows[csv_idx]
            if csv_idx in csv_pre_unmatched:
                reason = csv_pre_unmatched[csv_idx]
            elif row["label"] not in found_labels:
                reason = (
                    "GEEN_MATCH_BINNEN_EXTENT"
                    if extent is not None
                    else "GEEN_EXACT_LABEL_IN_WFS"
                )
            else:
                reason = "GEEN_WFS_LIJN_OVER_IN_LABELGROEP"

            feature = QgsFeature(unmatched_fields)
            values = [row["values"].get(name, "") for name in csv_fieldnames]
            values.extend([row["row_number"], row["label"], row["length_m"], reason])
            feature.setAttributes(values)
            unmatched_sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)

        feedback.setProgress(100.0)
        feedback.pushInfo(
            "Klaar in low-resource modus ({0}): {1} WFS-lijn(en), {2} koppeling(en), "
            "{3} CSV-rij(en) zonder match.".format(
                type_name, total_wfs, total_matches, len(unmatched_indices)
            )
        )
        return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
