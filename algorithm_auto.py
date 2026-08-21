# -*- coding: utf-8 -*-
"""Bounded, low-resource Enexis WFS/CSV matcher for QGIS 4.2."""

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
    TYPE_HINT = "e_lv_map_cable"
    LABEL_FIELD = "label"
    EXTENT = "EXTENT"
    RD_EPSG = 28992
    RD_AUTHID = "EPSG:28992"

    # Hard safety limits: stop safely instead of exhausting RAM.
    LABELS_PER_REQUEST = 5
    MAX_FEATURES_PER_BATCH = 500
    MAX_FEATURES_IN_EXTENT = 500
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_METADATA_BYTES = 2 * 1024 * 1024
    HTTP_TIMEOUT_SECONDS = 30
    MAX_LABELS_WITHOUT_EXTENT = 20

    TYPE_KEY = "enexiskabel/wfs_type_name"
    GEOMETRY_KEY = "enexiskabel/wfs_geometry_field"

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr("Koppel Enexis WFS-kabels automatisch aan CSV (extent-first)")

    def shortHelpString(self):
        return self.tr(
            "Bij een gekozen schermextent haalt de tool eerst uitsluitend de WFS-kabels "
            "binnen die extent op. Pas daarna worden lokaal de exacte Kabel Subgroep-"
            "matches en 1-op-1 lengtematches met de CSV bepaald. Er draait maximaal één "
            "WFS-request tegelijk en te grote responses worden veilig afgebroken."
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

    def _get_bytes(self, params, limit):
        request = Request(
            self.WFS_URL + "?" + urlencode(params),
            headers={"User-Agent": "QGIS Enexis Kabelkoppeling"},
        )
        with urlopen(request, timeout=self.HTTP_TIMEOUT_SECONDS) as response:
            return self._read_bounded(response, limit)

    def _discover_type_name(self, feedback):
        try:
            xml_bytes = self._get_bytes(
                {
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetCapabilities",
                },
                self.MAX_METADATA_BYTES,
            )
            root = ElementTree.fromstring(xml_bytes)
        except Exception as exc:
            raise DirectWfsError(
                "Kon Enexis WFS GetCapabilities niet lezen: " + str(exc)
            ) from exc

        names = []
        for feature_type in root.findall(".//{*}FeatureType"):
            node = feature_type.find("{*}Name")
            if node is not None and node.text:
                names.append(node.text.strip())

        needle = self.TYPE_HINT.lower()
        candidates = [name for name in names if needle in name.lower()]
        if not candidates:
            raise DirectWfsError(
                "Geen Enexis WFS-laag gevonden waarvan de naam 'e_lv_map_cable' bevat."
            )
        exact = [name for name in candidates if name.split(":")[-1].lower() == needle]
        chosen = min(exact or candidates, key=lambda value: (len(value), value))
        self._set_setting(self.TYPE_KEY, chosen)
        feedback.pushInfo("WFS-laagnaam opgeslagen: " + chosen)
        return chosen

    def _discover_geometry_field(self, type_name, feedback):
        cached = self._get_setting(self.GEOMETRY_KEY)
        if cached:
            return cached
        try:
            xml_bytes = self._get_bytes(
                {
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "DescribeFeatureType",
                    "typeNames": type_name,
                },
                self.MAX_METADATA_BYTES,
            )
            root = ElementTree.fromstring(xml_bytes)
        except Exception as exc:
            feedback.pushInfo("DescribeFeatureType mislukt: " + str(exc))
            return None

        for element in root.iter():
            if element.tag.split("}")[-1].lower() != "element":
                continue
            name = (element.attrib.get("name") or "").strip()
            descriptor = (
                (element.attrib.get("type") or "")
                + " "
                + (element.attrib.get("ref") or "")
            ).lower()
            if name and "gml" in descriptor and any(
                token in descriptor
                for token in ("line", "curve", "geometry", "multicurve")
            ):
                self._set_setting(self.GEOMETRY_KEY, name)
                feedback.pushInfo("WFS-geometrieveld opgeslagen: " + name)
                return name
        return None

    @staticmethod
    def _escape_cql(value):
        return str(value).replace("'", "''")

    def _label_cql_filter(self, labels):
        raw_values = set()
        for label in labels:
            raw_values.add(label)
            raw_values.add("Kabelgroup: " + label)
        quoted = ",".join(
            "'" + self._escape_cql(value) + "'" for value in sorted(raw_values)
        )
        return f"{self.LABEL_FIELD} IN ({quoted})"

    def _base_getfeature_params(self, type_name, geometry_field, max_features):
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "outputFormat": "application/json",
            "srsName": self.RD_AUTHID,
            "count": str(max_features + 1),
        }
        if geometry_field:
            params["propertyName"] = f"{self.LABEL_FIELD},{geometry_field}"
        return params

    def _extent_getfeature_url(self, type_name, geometry_field, extent):
        params = self._base_getfeature_params(
            type_name, geometry_field, self.MAX_FEATURES_IN_EXTENT
        )
        # Extent mode intentionally has NO CSV/label filter. First fetch the
        # cables in view, then compare those cables to the CSV locally.
        params["bbox"] = "{0},{1},{2},{3},{4}".format(
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
            self.RD_AUTHID,
        )
        return self.WFS_URL + "?" + urlencode(params)

    def _label_getfeature_url(self, type_name, geometry_field, labels):
        params = self._base_getfeature_params(
            type_name, geometry_field, self.MAX_FEATURES_PER_BATCH
        )
        params["cql_filter"] = self._label_cql_filter(labels)
        return self.WFS_URL + "?" + urlencode(params)

    def _http_geojson(self, url):
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
            raise DirectWfsError("WFS gaf geen GeoJSON terug: " + text.strip()[:500])
        return text

    def _parse_features(self, text, max_features):
        fields = QgsFields()
        fields.append(QgsField(self.LABEL_FIELD, QMetaType.Type.QString))
        features = QgsJsonUtils.stringToFeatureList(text, fields)
        if len(features) > max_features:
            raise DirectWfsError(
                "Meer dan {0} kabels binnen de opvraag. Zoom verder in of kies een "
                "kleinere extent.".format(max_features)
            )
        return features

    def _download_extent(self, type_name, geometry_field, extent):
        text = self._http_geojson(
            self._extent_getfeature_url(type_name, geometry_field, extent)
        )
        try:
            return self._parse_features(text, self.MAX_FEATURES_IN_EXTENT)
        finally:
            del text

    def _download_label_batch(self, type_name, geometry_field, labels):
        text = self._http_geojson(
            self._label_getfeature_url(type_name, geometry_field, labels)
        )
        try:
            return self._parse_features(text, self.MAX_FEATURES_PER_BATCH)
        finally:
            del text

    def _resolve_extent(self, extent, feedback):
        type_name = self._get_setting(self.TYPE_KEY) or self.TYPE_HINT
        geometry_field = self._get_setting(self.GEOMETRY_KEY) or None

        if not geometry_field:
            geometry_field = self._discover_geometry_field(type_name, feedback)
        if not geometry_field:
            type_name = self._discover_type_name(feedback)
            self._set_setting(self.GEOMETRY_KEY, "")
            geometry_field = self._discover_geometry_field(type_name, feedback)
        if not geometry_field:
            raise DirectWfsError(
                "Geometrieveld van e_lv_map_cable kon niet worden bepaald."
            )

        try:
            features = self._download_extent(type_name, geometry_field, extent)
            self._set_setting(self.TYPE_KEY, type_name)
            return type_name, geometry_field, features
        except DirectWfsError as first_error:
            if "te groot" in str(first_error) or "Meer dan" in str(first_error):
                raise
            self._set_setting(self.TYPE_KEY, "")
            self._set_setting(self.GEOMETRY_KEY, "")
            type_name = self._discover_type_name(feedback)
            geometry_field = self._discover_geometry_field(type_name, feedback)
            if not geometry_field:
                raise first_error
            return type_name, geometry_field, self._download_extent(
                type_name, geometry_field, extent
            )

    def _resolve_first_label_batch(self, labels, feedback):
        type_name = self._get_setting(self.TYPE_KEY) or self.TYPE_HINT
        geometry_field = self._get_setting(self.GEOMETRY_KEY) or None
        try:
            features = self._download_label_batch(type_name, geometry_field, labels)
            self._set_setting(self.TYPE_KEY, type_name)
            return type_name, geometry_field, features
        except DirectWfsError as first_error:
            if "te groot" in str(first_error) or "Meer dan" in str(first_error):
                raise
            self._set_setting(self.TYPE_KEY, "")
            type_name = self._discover_type_name(feedback)
            geometry_field = self._get_setting(self.GEOMETRY_KEY) or None
            return type_name, geometry_field, self._download_label_batch(
                type_name, geometry_field, labels
            )

    @staticmethod
    def _chunks(values, size):
        values = list(values)
        return [values[pos : pos + size] for pos in range(0, len(values), size)]

    @staticmethod
    def _wkb_type(features):
        for feature in features:
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                return geometry.wkbType()
        return Qgis.WkbType.LineString

    def _process_features(
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
    ):
        labels = []
        lengths = []
        line_groups = defaultdict(list)

        for index, feature in enumerate(features):
            label = normalize_label(feature[self.LABEL_FIELD])
            geometry = feature.geometry()
            length_m = (
                None
                if geometry is None or geometry.isEmpty()
                else round(geometry.length(), 2)
            )
            labels.append(label)
            lengths.append(length_m)
            if label:
                found_labels.add(label)
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

        for idx, feature in enumerate(features):
            geometry = feature.geometry()
            label = labels[idx]
            line_len = lengths[idx]
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

        return len(features), len(matches)

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
            extent = self.parameterAsExtent(parameters, self.EXTENT, context, source_crs)
            if extent is None or extent.isNull() or extent.isEmpty():
                extent = None

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
        type_name = self._get_setting(self.TYPE_KEY) or self.TYPE_HINT
        geometry_field = self._get_setting(self.GEOMETRY_KEY) or None
        matched_csv = set()
        found_labels = set()
        total_wfs = 0
        total_matches = 0

        if extent is not None:
            feedback.pushInfo(
                "Extent-first modus: eerst alleen WFS-kabels binnen het kaartvenster "
                "ophalen; daarna lokaal vergelijken met de CSV."
            )
            feedback.setProgressText("WFS-kabels binnen schermextent ophalen...")
            try:
                type_name, geometry_field, features = self._resolve_extent(
                    extent, feedback
                )
            except DirectWfsError as exc:
                raise QgsProcessingException(str(exc)) from exc

            sink, dest_id = self.parameterAsSink(
                parameters,
                self.OUTPUT,
                context,
                output_fields,
                self._wkb_type(features),
                source_crs,
            )
            if sink is None:
                raise QgsProcessingException(
                    self.invalidSinkError(parameters, self.OUTPUT)
                )

            if features:
                total_wfs, total_matches = self._process_features(
                    features,
                    csv_groups,
                    csv_rows,
                    output_fields,
                    csv_output_names,
                    aux_names,
                    sink,
                    matched_csv,
                    found_labels,
                )
            del features
            gc.collect()
            feedback.setProgress(80.0)

        else:
            batches = self._chunks(sorted(csv_groups.keys()), self.LABELS_PER_REQUEST)
            feedback.pushInfo(
                "Geen extent gekozen: veilige labelmodus met {0} batch(es), één request "
                "tegelijk.".format(len(batches))
            )

            for batch_index, labels in enumerate(batches):
                if feedback.isCanceled():
                    break
                feedback.setProgressText(
                    "WFS-batch {0}/{1} ophalen...".format(
                        batch_index + 1, len(batches)
                    )
                )
                try:
                    if batch_index == 0:
                        type_name, geometry_field, features = (
                            self._resolve_first_label_batch(labels, feedback)
                        )
                    else:
                        features = self._download_label_batch(
                            type_name, geometry_field, labels
                        )
                except DirectWfsError as exc:
                    raise QgsProcessingException(str(exc)) from exc

                if sink is None:
                    sink, dest_id = self.parameterAsSink(
                        parameters,
                        self.OUTPUT,
                        context,
                        output_fields,
                        self._wkb_type(features),
                        source_crs,
                    )
                    if sink is None:
                        raise QgsProcessingException(
                            self.invalidSinkError(parameters, self.OUTPUT)
                        )

                if features:
                    processed, matched = self._process_features(
                        features,
                        csv_groups,
                        csv_rows,
                        output_fields,
                        csv_output_names,
                        aux_names,
                        sink,
                        matched_csv,
                        found_labels,
                    )
                    total_wfs += processed
                    total_matches += matched

                del features
                gc.collect()
                feedback.setProgress(
                    80.0 * (batch_index + 1) / max(1, len(batches))
                )

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
            "Klaar ({0}): {1} WFS-lijn(en), {2} koppeling(en), {3} CSV-rij(en) zonder match.".format(
                type_name, total_wfs, total_matches, len(unmatched_indices)
            )
        )
        return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
