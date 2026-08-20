# -*- coding: utf-8 -*-
"""Shared helpers for the automatic Enexis WFS/CSV processing algorithm."""

from __future__ import annotations

import csv

from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    QgsField,
    QgsFields,
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingException,
)

from .matching import normalize_label, parse_decimal


# QGIS 4.2 / Qt6 native enum and field types.
FILE_BEHAVIOR = Qgis.ProcessingFileParameterBehavior.File
NO_GEOMETRY = Qgis.WkbType.NoGeometry


class KoppelWfsCsvAlgorithm(QgsProcessingAlgorithm):
    """Base class containing helpers shared by the automatic algorithm."""

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
            fields.append(QgsField(unique, QMetaType.Type.QString))
            csv_output_names[csv_name] = unique

        aux_names = {}
        aux_specs = [
            ("match_status", QMetaType.Type.QString),
            ("wfs_label_norm", QMetaType.Type.QString),
            ("wfs_len_m", QMetaType.Type.Double),
            ("csv_len_m", QMetaType.Type.Double),
            ("len_diff_m", QMetaType.Type.Double),
            ("csv_row_nr", QMetaType.Type.Int),
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
            fields.append(QgsField(name, QMetaType.Type.QString))
        fields.append(QgsField("csv_rij_nr", QMetaType.Type.Int))
        fields.append(QgsField("csv_label_norm", QMetaType.Type.QString))
        fields.append(QgsField("csv_len_m", QMetaType.Type.Double))
        fields.append(QgsField("reden", QMetaType.Type.QString))
        return fields
