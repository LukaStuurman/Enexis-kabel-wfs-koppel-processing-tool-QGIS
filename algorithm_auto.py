# -*- coding: utf-8 -*-
"""Automatic Enexis WFS variant of the cable/CSV matching algorithm."""

from __future__ import annotations

from collections import defaultdict
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from qgis.core import (
    QgsExpression,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsVectorLayer,
)

from .algorithm import FILE_BEHAVIOR, KoppelWfsCsvAlgorithm, NO_GEOMETRY
from .matching import normalize_label, optimal_one_to_one


class KoppelWfsCsvAutoAlgorithm(KoppelWfsCsvAlgorithm):
    """Processing algorithm that finds and loads Enexis e_lv_map_cable itself."""

    WFS_URL = "https://opendata.enexis.nl/geoserver/wfs"
    TYPE_NAME_CONTAINS = "e_lv_map_cable"

    def name(self):
        return "koppel_enexis_wfs_kabels_automatisch_aan_csv"

    def displayName(self):
        return self.tr("Koppel Enexis WFS-kabels automatisch aan CSV (1-op-1)")

    def shortHelpString(self):
        return self.tr(
            "Je kiest alleen de CSV. De tool vraagt automatisch de Enexis WFS-service op, "
            "zoekt de featuretype-naam die 'e_lv_map_cable' bevat, laadt die WFS-laag, "
            "zoekt automatisch het veld 'label' en haalt alleen kabels op waarvan het label "
            "in de CSV voorkomt. Daarna wordt exact op Kabel Subgroep en strikt 1-op-1 op "
            "de dichtstbijzijnde lengte gekoppeld."
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
        capabilities_url = (
            self.WFS_URL
            + "?service=WFS&request=GetCapabilities&version=2.0.0"
        )
        request = Request(capabilities_url, headers={"User-Agent": "QGIS Enexis Kabelkoppeling"})
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
                "Geen Enexis WFS-laag gevonden waarvan de naam 'e_lv_map_cable' bevat."
            )

        exact_local = [
            name for name in candidates if name.split(":")[-1].lower() == needle
        ]
        chosen = sorted(exact_local or candidates, key=lambda value: (len(value), value))[0]
        if len(candidates) > 1:
            feedback.pushInfo(
                "Meerdere WFS-lagen met 'e_lv_map_cable' gevonden; gekozen: " + chosen
            )
        else:
            feedback.pushInfo("Automatisch gevonden WFS-laag: " + chosen)
        return chosen

    def _load_wfs_layer(self, type_name):
        uri = "url='{0}' typename='{1}' version='auto'".format(
            self.WFS_URL, type_name.replace("'", "''")
        )
        layer = QgsVectorLayer(uri, type_name, "WFS")
        if not layer.isValid():
            raise QgsProcessingException(
                "De automatisch gevonden Enexis WFS-laag kon niet in QGIS worden geladen: "
                + type_name
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
            "De automatisch gevonden WFS-laag bevat geen veld met 'label' in de naam. "
            "Beschikbare velden: " + ", ".join(names)
        )

    @staticmethod
    def _label_filter_expression(label_field, csv_rows):
        # Match both the raw CSV subgroep and the normal Enexis prefix form.
        # QgsFeatureRequest lets the WFS provider push this filter to the server,
        # avoiding a download of the complete nationwide cable layer.
        raw_values = set()
        for row in csv_rows:
            label = row["label"]
            if label:
                raw_values.add(label)
                raw_values.add("Kabelgroup: " + label)
        if not raw_values:
            return None

        quoted_field = QgsExpression.quotedColumnRef(label_field)
        quoted_values = ", ".join(
            QgsExpression.quotedValue(value) for value in sorted(raw_values)
        )
        return f"{quoted_field} IN ({quoted_values})"

    def processAlgorithm(self, parameters, context, feedback):
        csv_path = self.parameterAsFile(parameters, self.CSV_FILE, context)
        csv_fieldnames, csv_rows = self._read_csv(csv_path)

        feedback.pushInfo("Enexis WFS-laag automatisch zoeken via GetCapabilities...")
        type_name = self._discover_type_name(feedback)
        source = self._load_wfs_layer(type_name)
        label_field = self._detect_label_field(source)
        feedback.pushInfo("Automatisch WFS-labelveld gevonden: " + label_field)

        output_fields, csv_output_names, aux_names = self._output_fields(
            source.fields(), csv_fieldnames
        )
        sink, dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            output_fields,
            source.wkbType(),
            source.sourceCrs(),
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
            source.sourceCrs(),
        )
        if unmatched_sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.UNMATCHED_CSV))

        csv_groups = defaultdict(list)
        csv_pre_unmatched = {}
        for csv_idx, row in enumerate(csv_rows):
            if not row["label"]:
                csv_pre_unmatched[csv_idx] = "LEGE_KABEL_SUBGROEP"
            elif row["length_m"] is None:
                csv_pre_unmatched[csv_idx] = row["length_error"]
            else:
                csv_groups[row["label"]].append((csv_idx, row["length_m"]))

        filter_expression = self._label_filter_expression(label_field, csv_rows)
        feature_request = QgsFeatureRequest()
        if filter_expression:
            feature_request.setFilterExpression(filter_expression)
            feedback.pushInfo("Alleen WFS-kabelgroepen uit de CSV worden opgevraagd.")

        line_records = []
        line_groups = defaultdict(list)
        for feature in source.getFeatures(feature_request):
            if feedback.isCanceled():
                break
            label = normalize_label(feature[label_field])
            try:
                length_m = self._length_m(
                    feature.geometry(), source.sourceCrs(), context.transformContext()
                )
            except Exception:
                length_m = None

            record = {
                "feature": feature,
                "label": label,
                "length_m": length_m,
            }
            line_idx = len(line_records)
            line_records.append(record)
            if label and length_m is not None:
                line_groups[label].append((line_idx, length_m))

        matches = {}
        matched_csv = set()
        common_labels = sorted(set(line_groups).intersection(csv_groups))
        for label in common_labels:
            if feedback.isCanceled():
                break
            pairs = optimal_one_to_one(line_groups[label], csv_groups[label])
            for line_idx, csv_idx in pairs:
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)

        output_index = {field.name(): i for i, field in enumerate(output_fields)}
        source_field_count = len(source.fields())

        for idx, record in enumerate(line_records):
            if feedback.isCanceled():
                break

            source_feature = record["feature"]
            out = QgsFeature(output_fields)
            out.setGeometry(source_feature.geometry())
            attrs = list(source_feature.attributes()) + [None] * (
                len(output_fields) - source_field_count
            )

            label = record["label"]
            line_len = record["length_m"]
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
                for csv_name, output_name in csv_output_names.items():
                    attrs[output_index[output_name]] = row["values"].get(csv_name, "")
                attrs[output_index[aux_names["csv_len_m"]]] = row["length_m"]
                attrs[output_index[aux_names["len_diff_m"]]] = round(
                    abs(line_len - row["length_m"]), 2
                )
                attrs[output_index[aux_names["csv_row_nr"]]] = row["row_number"]

            attrs[output_index[aux_names["match_status"]]] = status
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.FastInsert)

        unmatched_indices = set(range(len(csv_rows))) - matched_csv
        for csv_idx in sorted(unmatched_indices):
            row = csv_rows[csv_idx]
            if csv_idx in csv_pre_unmatched:
                reason = csv_pre_unmatched[csv_idx]
            elif row["label"] not in line_groups:
                reason = "GEEN_EXACT_LABEL_IN_WFS"
            else:
                reason = "GEEN_WFS_LIJN_OVER_IN_LABELGROEP"

            feature = QgsFeature(unmatched_fields)
            values = [row["values"].get(name, "") for name in csv_fieldnames]
            values.extend(
                [row["row_number"], row["label"], row["length_m"], reason]
            )
            feature.setAttributes(values)
            unmatched_sink.addFeature(feature, QgsFeatureSink.FastInsert)

        feedback.pushInfo(
            "Klaar met automatische WFS-laag {0}: {1} koppeling(en), {2} WFS-lijn(en) "
            "zonder CSV-match en {3} CSV-rij(en) zonder WFS-match.".format(
                type_name,
                len(matches),
                len(line_records) - len(matches),
                len(unmatched_indices),
            )
        )
        return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
