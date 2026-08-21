# -*- coding: utf-8 -*-
"""Two-stage, low-resource Enexis WFS/CSV matcher for QGIS 4.2."""

from __future__ import annotations

import gc
import json
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
    """First scan labels in the extent, then fetch geometry only for CSV matches."""

    WFS_URL = "https://opendata.enexis.nl/geoserver/wfs"
    TYPE_HINT = "e_lv_map_cable"
    EXTENT = "EXTENT"
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

    MAX_METADATA_BYTES = 2 * 1024 * 1024
    HTTP_TIMEOUT_SECONDS = 30
    TYPE_KEY = "enexiskabel/wfs_type_name"
    LABEL_KEY = "enexiskabel/wfs_label_field"

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr("Koppel Enexis WFS-kabels aan CSV (snelle extent-scan)")

    def shortHelpString(self):
        return self.tr(
            "Met een extent doet de tool eerst een lichte WFS-scan met uitsluitend het "
            "labelveld. Alleen labels die zowel in het kaartvenster als in de CSV voorkomen "
            "krijgen daarna een tweede geometrie-opvraag. Hierdoor worden niet meer alle "
            "kabelgeometrieën in het scherm gedownload. De tool gebruikt één request tegelijk."
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
            raise DirectWfsError(
                "WFS HTTP {0}: {1}".format(exc.code, detail or exc.reason)
            ) from exc
        except URLError as exc:
            raise DirectWfsError("WFS-netwerkfout: " + str(exc.reason)) from exc
        except Exception as exc:
            raise DirectWfsError("WFS-opvraag mislukt: " + str(exc)) from exc

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

    @staticmethod
    def _chunks(values, size):
        values = list(values)
        return [values[pos : pos + size] for pos in range(0, len(values), size)]

    def _fetch_geometry_records(self, type_name, label_field, labels, extent):
        params = self._getfeature_params(
            type_name, self.MAX_GEOMETRY_FEATURES_PER_BATCH + 1
        )
        params["cql_filter"] = self._label_filter(label_field, labels)
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
        return len(records), len(matches)

    @staticmethod
    def _sample(values, limit=12):
        items = sorted(str(value) for value in values if value)
        return ", ".join(items[:limit]) if items else "(geen)"

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

        output_fields, csv_output_names, aux_names = self._output_fields(
            QgsFields(), csv_fieldnames
        )
        sink, dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            output_fields,
            Qgis.WkbType.LineString,
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

        matched_csv = set()
        found_labels = set()
        total_geometry_records = 0
        total_matches = 0

        if csv_groups:
            feedback.setProgressText("WFS-schema en labelveld controleren...")
            try:
                type_name, label_field = self._resolve_type_and_label_field(
                    extent, feedback
                )

                if extent is not None:
                    feedback.setProgressText(
                        "Alleen WFS-labels binnen schermextent ophalen..."
                    )
                    extent_labels, scanned_count = self._fetch_extent_labels(
                        type_name, label_field, extent
                    )
                    found_labels.update(extent_labels)
                    common_labels = sorted(extent_labels & set(csv_groups.keys()))
                    feedback.pushInfo(
                        "Extent-scan: {0} kabeldeel-labels gelezen, {1} unieke labels, "
                        "{2} label(s) komen ook in de CSV voor.".format(
                            scanned_count, len(extent_labels), len(common_labels)
                        )
                    )
                else:
                    common_labels = sorted(csv_groups.keys())
                    found_labels.update(common_labels)
                    feedback.pushInfo(
                        "Geen extent gekozen: geometrie wordt alleen voor CSV-labels opgehaald."
                    )

                if not common_labels:
                    feedback.pushInfo(
                        "Geen exacte labelovereenkomst. WFS-voorbeeld: {0}".format(
                            self._sample(found_labels)
                        )
                    )
                    feedback.pushInfo(
                        "CSV-voorbeeld: {0}".format(self._sample(csv_groups.keys()))
                    )
                else:
                    batches = self._chunks(
                        common_labels, self.LABELS_PER_GEOMETRY_REQUEST
                    )
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
                        written, matched = self._write_records(
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

        unmatched_indices = [
            index for index in range(len(csv_rows)) if index not in matched_csv
        ]
        for csv_idx in unmatched_indices:
            row = csv_rows[csv_idx]
            if csv_idx in csv_pre_unmatched:
                reason = csv_pre_unmatched[csv_idx]
            elif extent is not None and row["label"] not in found_labels:
                reason = "GEEN_MATCH_BINNEN_EXTENT"
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
            "Klaar: {0} relevante WFS-geometrie(ën) verwerkt, {1} koppeling(en), "
            "{2} CSV-rij(en) zonder match.".format(
                total_geometry_records, total_matches, len(unmatched_indices)
            )
        )
        return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
