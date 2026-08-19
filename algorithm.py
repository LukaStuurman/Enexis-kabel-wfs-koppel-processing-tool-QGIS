# -*- coding: utf-8 -*-
"""Shared helpers for the automatic Enexis WFS/CSV processing algorithm."""

from __future__ import annotations

import csv

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsField,
    QgsFields,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFile,
    QgsUnitTypes,
    QgsWkbTypes,
)

from .matching import normalize_label, parse_decimal


# QGIS moved several enums after QGIS 3.28. Keep compatibility with both the
# classic QGIS 3.x API and newer enum layouts.
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
    """Base class containing only helpers shared by the automatic algorithm."""

    CSV_FILE = "CSV_FILE"
    OUTPUT = "OUTPUT"
    UNMATCHED_CSV = "UNMATCHED_CSV"

    CSV_LABEL_FIELD = "Kabel Subgroep"
    CSV_LENGTH_FIELD = "Lengte [kaart] (m)"

    def tr(self, text):
        return QCoreApplication.translate("KoppelWfsCsvAlgorithm", text)

    def group(self):
        return self.tr("Kabelkoppeling")

    def groupId(self):
        return "kabelkoppeling"

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
                        "values": {
                            name: (row.get(name) or "") for name in fieldnames
                        },
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

    @staticmethod
    def _unmatched_csv_fields(csv_fieldnames):
        fields = QgsFields()
        for name in csv_fieldnames:
            fields.append(QgsField(name, QVariant.String))
        fields.append(QgsField("csv_rij_nr", QVariant.Int))
        fields.append(QgsField("csv_label_norm", QVariant.String))
        fields.append(QgsField("csv_len_m", QVariant.Double))
        fields.append(QgsField("reden", QVariant.String))
        return fields
