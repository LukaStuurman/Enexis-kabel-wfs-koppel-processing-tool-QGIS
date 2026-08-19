# -*- coding: utf-8 -*-
"""Automatic Enexis WFS variant of the cable/CSV matching algorithm."""

from __future__ import annotations

from collections import defaultdict
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from qgis.core import (
    QgsDistanceArea,
    QgsExpression,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsUnitTypes,
    QgsVectorLayer,
)

from .algorithm import (
    DISTANCE_METERS,
    FILE_BEHAVIOR,
    KoppelWfsCsvAlgorithm,
    NO_GEOMETRY,
)
from .matching import normalize_label, optimal_one_to_one


class KoppelWfsCsvAutoAlgorithm(KoppelWfsCsvAlgorithm):
    """Processing algorithm that finds and loads Enexis e_lv_map_cable itself."""

    WFS_URL = "https://opendata.enexis.nl/geoserver/wfs"
    TYPE_NAME_CONTAINS = "e_lv_map_cable"
    _cached_type_name = None

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr("Koppel Enexis WFS-kabels automatisch aan CSV (1-op-1)")

    def shortHelpString(self):
        return self.tr(
            "Je kiest alleen de CSV. De tool zoekt automatisch de Enexis "
            "e_lv_map_cable WFS-laag, filtert de WFS zo vroeg mogelijk op de "
            "Kabel Subgroep-labels uit de CSV en koppelt daarna strikt 1-op-1 "
            "op de dichtstbijzijnde lengte."
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

    def _discover_type_name(self, feedback):
        cached = type(self)._cached_type_name
        if cached:
            feedback.pushInfo("WFS-laagnaam uit sessiecache: " + cached)
            return cached

        capabilities_url = (
            self.WFS_URL
            + "?service=WFS&request=GetCapabilities&version=2.0.0"
        )
        request = Request(
            capabilities_url,
            headers={"User-Agent": "QGIS Enexis Kabelkoppeling"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                xml_bytes = response.read()
        except Exception as exc:
            raise QgsProcessingException(
                "Kon Enexis WFS GetCapabilities niet ophalen: " + str(exc)
            ) from exc

        try:
            all_names = self._feature_type_names(xml_bytes)
        except Exception as exc:
            raise QgsProcessingException(
                "Kon Enexis WFS GetCapabilities niet lezen: " + str(exc)
            ) from exc

        needle = self.TYPE_NAME_CONTAINS.lower()
        candidates = [name for name in all_names if needle in name.lower()]
        if not candidates:
            raise QgsProcessingException(
                "Geen Enexis WFS-laag gevonden waarvan de naam "
                "'e_lv_map_cable' bevat."
            )

        exact_local = [
            name for name in candidates if name.split(":")[-1].lower() == needle
        ]
        chosen = min(
            exact_local or candidates,
            key=lambda value: (len(value), value),
        )
        type(self)._cached_type_name = chosen

        if len(candidates) > 1:
            feedback.pushInfo(
                "Meerdere WFS-lagen met 'e_lv_map_cable' gevonden; gekozen: "
                + chosen
            )
        else:
            feedback.pushInfo("Automatisch gevonden WFS-laag: " + chosen)
        return chosen

    def _load_wfs_layer(self, type_name):
        uri = "url='{0}' typename='{1}' version='auto'".format(
            self.WFS_URL,
            type_name.replace("'", "''"),
        )
        layer = QgsVectorLayer(uri, type_name, "WFS")
        if not layer.isValid():
            raise QgsProcessingException(
                "De automatisch gevonden Enexis WFS-laag kon niet in QGIS "
                "worden geladen: " + type_name
            )
        return layer

    @staticmethod
    def _detect_label_field(source):
        names = [field.name() for field in source.fields()]
        for name in names:
            if name.lower() == "label":
                return name
        for name in names:
            if "label" in name.lower():
                return name
        raise QgsProcessingException(
            "De automatisch gevonden WFS-laag bevat geen veld met 'label' "
            "in de naam. Beschikbare velden: " + ", ".join(names)
        )

    @staticmethod
    def _label_filter_expression(label_field, valid_labels):
        raw_values = set()
        for label in valid_labels:
            raw_values.add(label)
            raw_values.add("Kabelgroup: " + label)

        if not raw_values:
            return None

        quoted_field = QgsExpression.quotedColumnRef(label_field)
        quoted_values = ", ".join(
            QgsExpression.quotedValue(value) for value in sorted(raw_values)
        )
        return f"{quoted_field} IN ({quoted_values})"

    @staticmethod
    def _feature_request(source, filter_expression, feedback):
        """Prefer a provider subset; fall back to a feature request expression.

        A WFS provider subset is attached to the data provider itself and is
        therefore the strongest hint that filtering should happen remotely.
        The fallback keeps compatibility with providers/QGIS builds that do
        not expose subset strings.
        """
        if not filter_expression:
            return None

        provider = source.dataProvider()
        if provider is not None and provider.supportsSubsetString():
            try:
                applied = provider.setSubsetString(filter_expression, False)
            except TypeError:
                applied = provider.setSubsetString(filter_expression)
            except Exception:
                applied = False

            if applied:
                feedback.pushInfo(
                    "Kabelgroepfilter direct op de WFS-provider ingesteld."
                )
                return None

        request = QgsFeatureRequest()
        request.setFilterExpression(filter_expression)
        feedback.pushInfo(
            "WFS-provider accepteerde geen subsetfilter; "
            "QgsFeatureRequest-filter wordt gebruikt."
        )
        return request

    @staticmethod
    def _length_function(source_crs, transform_context):
        """Create a length function once instead of rebuilding helpers per feature."""
        if not source_crs.isGeographic():
            factor = QgsUnitTypes.fromUnitToUnitFactor(
                source_crs.mapUnits(),
                DISTANCE_METERS,
            )

            def projected_length(geometry):
                if geometry is None or geometry.isEmpty():
                    return None
                return round(geometry.length() * factor, 2)

            return projected_length

        distance = QgsDistanceArea()
        distance.setSourceCrs(source_crs, transform_context)
        ellipsoid = source_crs.ellipsoidAcronym()
        if ellipsoid:
            distance.setEllipsoid(ellipsoid)

        def geographic_length(geometry):
            if geometry is None or geometry.isEmpty():
                return None
            measured = distance.measureLength(geometry)
            return round(
                distance.convertLengthMeasurement(measured, DISTANCE_METERS),
                2,
            )

        return geographic_length

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

        feedback.pushInfo("Enexis WFS-laag automatisch zoeken...")
        type_name = self._discover_type_name(feedback)
        source = self._load_wfs_layer(type_name)

        source_fields = source.fields()
        source_crs = source.sourceCrs()
        source_wkb_type = source.wkbType()
        label_field = self._detect_label_field(source)
        feedback.pushInfo("Automatisch WFS-labelveld gevonden: " + label_field)

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

        if csv_groups:
            filter_expression = self._label_filter_expression(
                label_field,
                csv_groups.keys(),
            )
            feature_request = self._feature_request(
                source,
                filter_expression,
                feedback,
            )
            feature_iterator = (
                source.getFeatures(feature_request)
                if feature_request is not None
                else source.getFeatures()
            )

            measure_length = self._length_function(
                source_crs,
                context.transformContext(),
            )

            feedback.setProgressText("Relevante WFS-kabels ophalen...")
            for feature in feature_iterator:
                if feedback.isCanceled():
                    break

                label = normalize_label(feature[label_field])
                try:
                    length_m = measure_length(feature.geometry())
                except Exception:
                    length_m = None

                line_idx = len(line_records)
                line_records.append((feature, label, length_m))
                if label and length_m is not None:
                    line_groups[label].append((line_idx, length_m))
        else:
            feedback.pushInfo(
                "CSV bevat geen geldige Kabel Subgroep + lengte-combinaties; "
                "er worden geen WFS-features opgehaald."
            )

        feedback.setProgress(55.0)
        matches = {}
        matched_csv = set()

        common_labels = line_groups.keys() & csv_groups.keys()
        for label in common_labels:
            if feedback.isCanceled():
                break

            for line_idx, csv_idx in optimal_one_to_one(
                line_groups[label],
                csv_groups[label],
            ):
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)

        feedback.setProgress(70.0)
        output_index = {
            field.name(): i for i, field in enumerate(output_fields)
        }
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
                    attrs[output_index[output_name]] = row_values.get(
                        csv_name,
                        "",
                    )

                attrs[output_index[aux_names["csv_len_m"]]] = row["length_m"]
                attrs[output_index[aux_names["len_diff_m"]]] = round(
                    abs(line_len - row["length_m"]),
                    2,
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
                reason = "GEEN_EXACT_LABEL_IN_WFS"
            else:
                reason = "GEEN_WFS_LIJN_OVER_IN_LABELGROEP"

            feature = QgsFeature(unmatched_fields)
            values = [
                row["values"].get(name, "") for name in csv_fieldnames
            ]
            values.extend(
                [
                    row["row_number"],
                    row["label"],
                    row["length_m"],
                    reason,
                ]
            )
            feature.setAttributes(values)
            unmatched_sink.addFeature(feature, QgsFeatureSink.FastInsert)

        feedback.setProgress(100.0)
        feedback.pushInfo(
            "Klaar met automatische WFS-laag {0}: {1} koppeling(en), "
            "{2} WFS-lijn(en) zonder CSV-match en {3} CSV-rij(en) "
            "zonder WFS-match.".format(
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
