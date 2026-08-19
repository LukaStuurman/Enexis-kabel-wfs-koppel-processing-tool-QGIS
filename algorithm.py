# -*- coding: utf-8 -*-
"""QGIS Processing algorithm for one-to-one Enexis WFS/CSV cable matching."""

from __future__ import annotations

import csv
from collections import defaultdict

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFeatureSource,
    QgsProject,
    QgsUnitTypes,
    QgsWkbTypes,
)

from .matching import normalize_label, optimal_one_to_one, parse_decimal


# QGIS moved several Processing/WKB/unit enums after QGIS 3.28. Keep this
# plugin usable on both the 3.x API and newer API layouts.
try:
    VECTOR_LINE_SOURCE = Qgis.ProcessingSourceType.VectorLine
except AttributeError:
    VECTOR_LINE_SOURCE = QgsProcessing.TypeVectorLine

try:
    STRING_FIELD_TYPE = Qgis.ProcessingFieldParameterDataType.String
except AttributeError:
    STRING_FIELD_TYPE = QgsProcessingParameterField.String

try:
    FILE_BEHAVIOR = Qgis.ProcessingFileParameterBehavior.File
except AttributeError:
    FILE_BEHAVIOR = QgsProcessingParameterFile.File

try:
    DISTANCE_METERS = Qgis.DistanceUnit.Meters
except AttributeError:
    DISTANCE_METERS = QgsUnitTypes.DistanceMeters

try:
    NO_GEOMETRY = Qgis.WkbType.NoGeometry
except AttributeError:
    NO_GEOMETRY = QgsWkbTypes.NoGeometry


class KoppelWfsCsvAlgorithm(QgsProcessingAlgorithm):
    INPUT_LINES = "INPUT_LINES"
    LABEL_FIELD = "LABEL_FIELD"
    CSV_FILE = "CSV_FILE"
    OUTPUT = "OUTPUT"
    UNMATCHED_CSV = "UNMATCHED_CSV"

    CSV_LABEL_FIELD = "Kabel Subgroep"
    CSV_LENGTH_FIELD = "Lengte [kaart] (m)"

    def tr(self, text):
        return QCoreApplication.translate("KoppelWfsCsvAlgorithm", text)

    def name(self):
        return "koppel_wfs_kabels_aan_csv"

    def displayName(self):
        return self.tr("Koppel WFS-kabels aan CSV (1-op-1)")

    def group(self):
        return self.tr("Kabelkoppeling")

    def groupId(self):
        return "kabelkoppeling"

    def shortHelpString(self):
        return self.tr(
            "Koppelt Enexis WFS-lijnen één-op-één aan CSV-rijen. Eerst moet het "
            "WFS-label exact overeenkomen met 'Kabel Subgroep' nadat 'Kabelgroup: ' "
            "is verwijderd. Binnen elke exacte labelgroep wordt de lijnlengte in meter "
            "op 2 decimalen afgerond en gekoppeld aan de dichtstbijzijnde CSV-lengte. "
            "Dubbele kabelsubgroepen worden globaal optimaal en strikt één-op-één toegewezen."
        )

    def createInstance(self):
        return KoppelWfsCsvAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINES,
                self.tr("Enexis WFS-kabellijnen"),
                [VECTOR_LINE_SOURCE],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.LABEL_FIELD,
                self.tr("WFS-veld met label (bijv. label)"),
                parentLayerParameterName=self.INPUT_LINES,
                type=STRING_FIELD_TYPE,
                defaultValue="label",
            )
        )
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
                self.tr("Gekoppelde WFS-lijnen (alle lijnen + matchstatus)"),
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.UNMATCHED_CSV,
                self.tr("Niet-gekoppelde CSV-rijen"),
            )
        )

    @staticmethod
    def _detect_delimiter(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
        try:
            return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
        except csv.Error:
            return ";"

    def _read_csv(self, path):
        delimiter = self._detect_delimiter(path)
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            fieldnames = reader.fieldnames or []
            missing = [
                name
                for name in (self.CSV_LABEL_FIELD, self.CSV_LENGTH_FIELD)
                if name not in fieldnames
            ]
            if missing:
                raise QgsProcessingException(
                    "CSV mist verplichte kolom(men): " + ", ".join(missing)
                )

            rows = []
            for row_number, row in enumerate(reader, start=2):
                raw_length = row.get(self.CSV_LENGTH_FIELD)
                try:
                    length_m = round(parse_decimal(raw_length), 2)
                    length_error = ""
                except (TypeError, ValueError):
                    length_m = None
                    length_error = "ONGELDIGE_CSV_LENGTE"

                rows.append(
                    {
                        "row_number": row_number,
                        "values": {name: (row.get(name) or "") for name in fieldnames},
                        "label": normalize_label(row.get(self.CSV_LABEL_FIELD)),
                        "length_m": length_m,
                        "length_error": length_error,
                    }
                )
        return fieldnames, rows

    @staticmethod
    def _unique_field_name(fields, wanted):
        existing = {field.name().lower() for field in fields}
        candidate = wanted
        counter = 2
        while candidate.lower() in existing:
            candidate = f"{wanted}_{counter}"
            counter += 1
        return candidate

    def _output_fields(self, source_fields, csv_fieldnames):
        fields = QgsFields(source_fields)
        csv_output_names = {}
        for csv_name in csv_fieldnames:
            wanted = "csv_" + csv_name
            unique = self._unique_field_name(fields, wanted)
            fields.append(QgsField(unique, QVariant.String))
            csv_output_names[csv_name] = unique

        aux_names = {}
        aux_specs = [
            ("match_status", QVariant.String),
            ("wfs_label_norm", QVariant.String),
            ("wfs_len_m", QVariant.Double),
            ("csv_len_m", QVariant.Double),
            ("len_diff_m", QVariant.Double),
            ("csv_row_nr", QVariant.Int),
        ]
        for wanted, field_type in aux_specs:
            unique = self._unique_field_name(fields, wanted)
            fields.append(QgsField(unique, field_type))
            aux_names[wanted] = unique
        return fields, csv_output_names, aux_names

    def _unmatched_csv_fields(self, csv_fieldnames):
        fields = QgsFields()
        for name in csv_fieldnames:
            fields.append(QgsField(name, QVariant.String))
        fields.append(QgsField("csv_rij_nr", QVariant.Int))
        fields.append(QgsField("csv_label_norm", QVariant.String))
        fields.append(QgsField("csv_len_m", QVariant.Double))
        fields.append(QgsField("reden", QVariant.String))
        return fields

    def _length_m(self, geometry, source_crs, transform_context):
        if geometry is None or geometry.isEmpty():
            return None

        # "Lengte [kaart]" is a map/geometry length. For projected WFS data
        # (e.g. Dutch RD New) use the planar geometry length and only convert
        # the CRS map unit to meters. This avoids introducing ellipsoidal
        # differences which are not present in the map length.
        if not source_crs.isGeographic():
            factor = QgsUnitTypes.fromUnitToUnitFactor(
                source_crs.mapUnits(), DISTANCE_METERS
            )
            return round(geometry.length() * factor, 2)

        # Geographic CRS: measure geodesically so degrees are not treated as
        # linear units. The result is converted to meters before rounding.
        distance = QgsDistanceArea()
        distance.setSourceCrs(source_crs, transform_context)
        ellipsoid = QgsProject.instance().ellipsoid()
        if ellipsoid:
            distance.setEllipsoid(ellipsoid)
        measured = distance.measureLength(geometry)
        return round(distance.convertLengthMeasurement(measured, DISTANCE_METERS), 2)

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT_LINES, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINES))

        label_field = self.parameterAsString(parameters, self.LABEL_FIELD, context)
        if source.fields().indexOf(label_field) < 0:
            raise QgsProcessingException(f"WFS-labelveld bestaat niet: {label_field}")

        csv_path = self.parameterAsFile(parameters, self.CSV_FILE, context)
        csv_fieldnames, csv_rows = self._read_csv(csv_path)

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

        line_records = []
        line_groups = defaultdict(list)
        total = source.featureCount()
        for idx, feature in enumerate(source.getFeatures()):
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
            line_records.append(record)
            if label and length_m is not None:
                line_groups[label].append((idx, length_m))

            if total:
                feedback.setProgress(min(45.0, (idx + 1) * 45.0 / total))

        matches = {}
        matched_csv = set()
        common_labels = sorted(set(line_groups).intersection(csv_groups))
        for pos, label in enumerate(common_labels):
            if feedback.isCanceled():
                break
            pairs = optimal_one_to_one(line_groups[label], csv_groups[label])
            for line_idx, csv_idx in pairs:
                matches[line_idx] = csv_idx
                matched_csv.add(csv_idx)
            if common_labels:
                feedback.setProgress(45.0 + (pos + 1) * 20.0 / len(common_labels))

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

            if line_records:
                feedback.setProgress(65.0 + (idx + 1) * 25.0 / len(line_records))

        unmatched_indices = set(range(len(csv_rows))) - matched_csv
        for pos, csv_idx in enumerate(sorted(unmatched_indices)):
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

            if unmatched_indices:
                feedback.setProgress(90.0 + (pos + 1) * 10.0 / len(unmatched_indices))

        feedback.pushInfo(
            f"Klaar: {len(matches)} koppeling(en), "
            f"{len(line_records) - len(matches)} WFS-lijn(en) zonder CSV-match en "
            f"{len(unmatched_indices)} CSV-rij(en) zonder WFS-match."
        )
        return {self.OUTPUT: dest_id, self.UNMATCHED_CSV: unmatched_id}
