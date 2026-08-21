# -*- coding: utf-8 -*-

"""
***************************************************************************
*                                                                         *
* Exporteert kabels naar DXF-bestanden op basis van projectcodes.         *
*                                                                         *
* UPDATE V6 (LANDELIJKE STREAMING):                                       *
* - Werkt direct met de uitvoer van de Enexis WFS-CSV-koppeltool v0.9.0.  *
* - Gebruikt standaard wfs_label_norm en csv_Type.                        *
* - Herkent zowel "BEK4020-04" als "Kabelgroup: BEK4020-04".             *
* - Valt automatisch terug op oudere veldnamen.                           *
* - Houdt de ruimtelijke selectie + zoekradius uit V4.                    *
* - Schrijft heel Nederland direct naar begrensde DXF-delen.              *
*                                                                         *
***************************************************************************
"""

import os
import re
import zlib

from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterField,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsRectangle,
    QgsVectorFileWriter,
)


class SplitNaarDXF(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    FIELD = "FIELD"
    TYPE_FIELD = "TYPE_FIELD"
    WHOLE_COUNTRY = "WHOLE_COUNTRY"
    ONLY_MATCHED = "ONLY_MATCHED"
    FEATURES_PER_DXF = "FEATURES_PER_DXF"
    USE_SELECTION = "USE_SELECTION"
    SEARCH_RADIUS = "SEARCH_RADIUS"
    SEARCH_TERMS = "SEARCH_TERMS"
    EXTENDED_LAYER_NAMES = "EXTENDED_LAYER_NAMES"
    VARY_COLORS = "VARY_COLORS"
    SINGLE_FILE = "SINGLE_FILE"
    MERGE_LINES = "MERGE_LINES"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    FEATURES_EXPORTED = "FEATURES_EXPORTED"
    FILES_WRITTEN = "FILES_WRITTEN"

    # De eerste twee kandidaten zijn de velden uit de actuele koppeltool.
    LABEL_FIELD_CANDIDATES = (
        "wfs_label_norm",
        "csv_Kabel Subgroep",
        "Kabel Subgroep",
        "label",
        "csv_Label Tekst",
        "Label Tekst",
    )
    TYPE_FIELD_CANDIDATES = (
        "csv_Type",
        "Type",
        "KabelType",
        "Kabel Type",
    )
    STATUS_FIELD_CANDIDATES = ("match_status",)
    COLOR_MAP = (
        "#FF0000",
        "#00FF00",
        "#00FFFF",
        "#FF00FF",
        "#FFFF00",
        "#0000FF",
        "#FF7F00",
        "#FF69B4",
        "#008080",
        "#A52A2A",
    )

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return SplitNaarDXF()

    def name(self):
        return "splitnaardxf_v5_kabelkoppeling"

    def displayName(self):
        return self.tr("Split gekoppelde kabels naar DXF (V6 - landelijk)")

    def group(self):
        return self.tr("Kabelkoppeling")

    def groupId(self):
        return "kabelkoppeling"

    def shortHelpString(self):
        return self.tr(
            "Versie 6: geschikt voor de uitvoerlaag van de Enexis "
            "WFS-CSV-koppeltool.\n\n"
            "Standaardvelden:\n"
            "- groepering/label: wfs_label_norm\n"
            "- kabeltype: csv_Type\n\n"
            "Selecteer één of meer gekoppelde kabels. De tool haalt uit labels "
            "zoals BEK4020-04 automatisch projectcode BEK4020, zoekt binnen de "
            "ingestelde radius naar kabels met dezelfde code en exporteert deze "
            "naar DXF. Oudere velden zoals label en KabelType worden eveneens "
            "automatisch herkend.\n\n"
            "Voor heel Nederland schakel je 'Landelijke streamingmodus' in. "
            "De tool leest dan alleen de benodigde attributen, houdt geen "
            "geometrieverzameling in RAM en schrijft direct naar opeenvolgende "
            "DXF-delen. Samenvoegen van lijnen wordt in deze modus bewust "
            "overgeslagen om het geheugengebruik vrijwel constant te houden."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT,
                self.tr("Invoerlaag (uitkomst Enexis WFS-CSV-koppeling)"),
                [QgsProcessing.TypeVectorLine],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.FIELD,
                self.tr("Labelveld voor bestandsnaam/groepering"),
                "wfs_label_norm",
                self.INPUT,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.TYPE_FIELD,
                self.tr("Kabeltypeveld voor extra informatie in de laagnaam"),
                "csv_Type",
                self.INPUT,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.WHOLE_COUNTRY,
                self.tr(
                    "Landelijke streamingmodus (alle gekoppelde kabels exporteren)"
                ),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ONLY_MATCHED,
                self.tr("Alleen kabels met match_status = GEKOPPELD exporteren"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.FEATURES_PER_DXF,
                self.tr("Landelijke modus: maximaal aantal kabels per DXF-deel"),
                type=Qgis.ProcessingNumberParameterType.Integer,
                defaultValue=25000,
                minValue=1000,
                maxValue=500000,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_SELECTION,
                self.tr("Gebruik geselecteerde kabels voor zoektermen"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SEARCH_RADIUS,
                self.tr("Zoekradius om selectie (meters)"),
                type=Qgis.ProcessingNumberParameterType.Integer,
                defaultValue=3000,
                minValue=0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.SEARCH_TERMS,
                self.tr("Handmatige zoektermen (optioneel, gescheiden door komma)"),
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.EXTENDED_LAYER_NAMES,
                self.tr("Uitgebreide laagnamen (zoekterm + tekst erna meenemen)"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VARY_COLORS,
                self.tr("Wisselende kleuren per zoekterm gebruiken"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MERGE_LINES,
                self.tr("Lijnen met dezelfde laagnaam samenvoegen tot één lijn"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SINGLE_FILE,
                self.tr("Alles in één DXF-bestand opslaan"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                self.tr("Opslagmap voor DXF-bestanden"),
                defaultValue=(
                    r"C:\Users\LukaStuurmanHDBTechb\Techbase\Techbase - Documenten"
                    r"\Tekenkamer\Techbase WES\4. QGIS\Enexis Data GIS"
                    r"\Tijdelijke Bestanden"
                ),
            )
        )
        self.addOutput(
            QgsProcessingOutputNumber(
                self.FEATURES_EXPORTED,
                self.tr("Aantal geëxporteerde kabelobjecten"),
            )
        )
        self.addOutput(
            QgsProcessingOutputNumber(
                self.FILES_WRITTEN,
                self.tr("Aantal geschreven DXF-bestanden"),
            )
        )

    @staticmethod
    def _field_lookup(layer):
        return {field.name().casefold(): field.name() for field in layer.fields()}

    def _resolve_field(
        self, layer, requested_name, candidates, role, feedback, required=False
    ):
        lookup = self._field_lookup(layer)
        requested = (requested_name or "").strip()
        if requested and requested.casefold() in lookup:
            return lookup[requested.casefold()]

        for candidate in candidates:
            if candidate.casefold() in lookup:
                resolved = lookup[candidate.casefold()]
                if requested and requested.casefold() != resolved.casefold():
                    feedback.pushInfo(
                        "Veld '{0}' bestaat niet; voor {1} wordt automatisch "
                        "'{2}' gebruikt.".format(requested, role, resolved)
                    )
                return resolved

        if required:
            raise QgsProcessingException(
                "Geen geschikt {0} gevonden. Beschikbare velden: {1}".format(
                    role, ", ".join(field.name() for field in layer.fields())
                )
            )

        if requested:
            feedback.pushInfo(
                "Optioneel veld '{0}' bestaat niet; {1} wordt overgeslagen.".format(
                    requested, role
                )
            )
        return ""

    @staticmethod
    def _extract_search_term(value):
        """Maak BEK4020 uit zowel BEK4020-04 als Kabelgroup: BEK4020-04."""
        if value is None:
            return ""

        text = str(value).strip()
        if not text:
            return ""

        # De oude WFS-notatie kan nog in oudere lagen voorkomen.
        text = re.sub(r"^\s*Kabelgroup\s*:\s*", "", text, flags=re.IGNORECASE)

        if "-" in text:
            return text.split("-", 1)[0].strip()
        return text

    @staticmethod
    def _safe_name(value):
        return re.sub(r'[\\/*?:"<>|]', "_", str(value)).strip()

    @staticmethod
    def _type_suffix(type_value):
        if type_value is None:
            return ""
        type_text = str(type_value).strip()
        if not type_text:
            return ""
        type_matches = re.findall(
            r"x\s*(.*?(?:Al|Cu))", type_text, re.IGNORECASE
        )
        if type_matches:
            return " " + " + ".join(type_matches)
        return " " + type_text

    @classmethod
    def _color_for_term(cls, term):
        # crc32 is stabiel tussen QGIS-sessies; Python hash() is dat niet.
        color_index = zlib.crc32(term.casefold().encode("utf-8")) % len(
            cls.COLOR_MAP
        )
        return cls.COLOR_MAP[color_index]

    @staticmethod
    def _dxf_fields(vary_colors):
        fields = QgsFields()
        fields.append(QgsField("Layer", QMetaType.Type.QString))
        if vary_colors:
            fields.append(QgsField("OGR_STYLE", QMetaType.Type.QString))
        return fields

    def _layer_name(
        self, value_text, term_key, type_value, use_extended_names
    ):
        clean_term = self._safe_name(term_key)
        base_layer_name = clean_term
        if use_extended_names:
            specific_match = re.search(
                re.escape(term_key) + r".*", value_text, re.IGNORECASE
            )
            if specific_match:
                base_layer_name = specific_match.group(0)
        return self._safe_name(base_layer_name + self._type_suffix(type_value))

    @staticmethod
    def _create_dxf_writer(
        output_path, fields, source_layer, context
    ):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "DXF"
        options.fileEncoding = "UTF-8"
        writer = QgsVectorFileWriter.create(
            output_path,
            fields,
            source_layer.wkbType(),
            source_layer.crs(),
            context.transformContext(),
            options,
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            message = writer.errorMessage()
            del writer
            raise QgsProcessingException(
                "Fout bij maken van {0}: {1}".format(output_path, message)
            )
        return writer

    def _stream_whole_country(
        self,
        source_layer,
        field_name,
        type_field_name,
        status_field_name,
        only_matched,
        use_extended_names,
        vary_colors,
        features_per_file,
        output_folder,
        context,
        feedback,
    ):
        """Schrijf landelijke uitvoer direct en met vrijwel constant RAM."""
        feedback.pushInfo(
            "Landelijke streamingmodus actief: geen geometrieën worden in "
            "geheugen verzameld en lijnen worden niet samengevoegd."
        )
        feedback.pushInfo(
            "Ieder DXF-deel bevat maximaal {0} kabels.".format(features_per_file)
        )

        fields = self._dxf_fields(vary_colors)
        requested_names = [field_name]
        if type_field_name:
            requested_names.append(type_field_name)
        if status_field_name:
            requested_names.append(status_field_name)
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes(
            list(dict.fromkeys(requested_names)), source_layer.fields()
        )

        total_features = source_layer.featureCount()
        scanned_count = 0
        exported_features = 0
        skipped_status = 0
        skipped_invalid = 0
        files_written = 0
        features_in_file = 0
        writer = None
        current_path = ""

        try:
            for feature in source_layer.getFeatures(request):
                if feedback.isCanceled():
                    break

                scanned_count += 1
                if only_matched and status_field_name:
                    status = str(feature[status_field_name] or "").strip()
                    if status.casefold() != "gekoppeld":
                        skipped_status += 1
                        continue

                value = feature[field_name]
                value_text = "" if value is None else str(value).strip()
                term_key = self._extract_search_term(value_text)
                geometry = feature.geometry()
                if (
                    not term_key
                    or geometry is None
                    or geometry.isEmpty()
                ):
                    skipped_invalid += 1
                    continue

                if writer is None or features_in_file >= features_per_file:
                    if writer is not None:
                        del writer
                        writer = None
                        feedback.pushInfo("DXF opgeslagen: {0}".format(current_path))

                    files_written += 1
                    features_in_file = 0
                    current_path = os.path.join(
                        output_folder,
                        "Nederland_Kabels_{0:04d}.dxf".format(files_written),
                    )
                    writer = self._create_dxf_writer(
                        current_path, fields, source_layer, context
                    )

                type_value = (
                    feature[type_field_name] if type_field_name else None
                )
                layer_name = self._layer_name(
                    value_text,
                    term_key,
                    type_value,
                    use_extended_names,
                )
                out_feature = QgsFeature(fields)
                out_feature.setGeometry(QgsGeometry(geometry))
                out_feature.setAttribute("Layer", layer_name)
                if vary_colors:
                    out_feature.setAttribute(
                        "OGR_STYLE",
                        "PEN(c:{0})".format(self._color_for_term(term_key)),
                    )
                if not writer.addFeature(out_feature):
                    raise QgsProcessingException(
                        "Schrijven naar {0} is mislukt: {1}".format(
                            current_path, writer.lastError()
                        )
                    )

                exported_features += 1
                features_in_file += 1

                if scanned_count % 5000 == 0:
                    feedback.pushInfo(
                        "Landelijke export: {0} bekeken, {1} geschreven, "
                        "{2} DXF-deel/delen gestart.".format(
                            scanned_count, exported_features, files_written
                        )
                    )
                if total_features > 0 and scanned_count % 500 == 0:
                    feedback.setProgress(
                        min(99, int((scanned_count / total_features) * 100))
                    )
        finally:
            if writer is not None:
                del writer
                writer = None

        if current_path:
            feedback.pushInfo("DXF opgeslagen: {0}".format(current_path))
        feedback.setProgress(100)
        feedback.pushInfo(
            "Landelijke export gereed: {0} bekeken, {1} kabels geschreven "
            "naar {2} DXF-bestand(en), {3} op status overgeslagen en {4} met "
            "een leeg label/geometrie overgeslagen.".format(
                scanned_count,
                exported_features,
                files_written,
                skipped_status,
                skipped_invalid,
            )
        )
        return {
            self.FEATURES_EXPORTED: exported_features,
            self.FILES_WRITTEN: files_written,
        }

    def processAlgorithm(self, parameters, context, feedback):
        source_layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if source_layer is None:
            raise QgsProcessingException("De invoerlaag kon niet worden geopend.")

        requested_field = self.parameterAsString(parameters, self.FIELD, context)
        requested_type_field = self.parameterAsString(
            parameters, self.TYPE_FIELD, context
        )
        field_name = self._resolve_field(
            source_layer,
            requested_field,
            self.LABEL_FIELD_CANDIDATES,
            "labelveld",
            feedback,
            required=True,
        )
        type_field_name = self._resolve_field(
            source_layer,
            requested_type_field,
            self.TYPE_FIELD_CANDIDATES,
            "kabeltype",
            feedback,
        )

        whole_country = self.parameterAsBool(
            parameters, self.WHOLE_COUNTRY, context
        )
        only_matched = self.parameterAsBool(
            parameters, self.ONLY_MATCHED, context
        )
        features_per_file = max(
            1000,
            self.parameterAsInt(parameters, self.FEATURES_PER_DXF, context),
        )
        status_field_name = ""
        if only_matched:
            status_field_name = self._resolve_field(
                source_layer,
                "match_status",
                self.STATUS_FIELD_CANDIDATES,
                "koppelstatus",
                feedback,
            )
            if not status_field_name:
                feedback.pushInfo(
                    "Geen match_status-veld gevonden; alle kabels met een geldig "
                    "label en geometrie worden geëxporteerd."
                )

        use_selection = self.parameterAsBool(
            parameters, self.USE_SELECTION, context
        )
        search_radius = self.parameterAsInt(
            parameters, self.SEARCH_RADIUS, context
        )
        manual_search_string = self.parameterAsString(
            parameters, self.SEARCH_TERMS, context
        )
        use_extended_names = self.parameterAsBool(
            parameters, self.EXTENDED_LAYER_NAMES, context
        )
        vary_colors = self.parameterAsBool(
            parameters, self.VARY_COLORS, context
        )
        merge_lines = self.parameterAsBool(
            parameters, self.MERGE_LINES, context
        )
        single_file = self.parameterAsBool(
            parameters, self.SINGLE_FILE, context
        )
        output_folder = self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context
        )

        if not output_folder:
            raise QgsProcessingException("Kies een opslagmap voor de DXF-bestanden.")
        os.makedirs(output_folder, exist_ok=True)

        feedback.pushInfo("Labelveld: {0}".format(field_name))
        feedback.pushInfo(
            "Kabeltypeveld: {0}".format(type_field_name or "(niet gebruikt)")
        )

        if whole_country:
            return self._stream_whole_country(
                source_layer,
                field_name,
                type_field_name,
                status_field_name,
                only_matched,
                use_extended_names,
                vary_colors,
                features_per_file,
                output_folder,
                context,
                feedback,
            )

        # STAP 1: zoektermen en selectiegebied bepalen.
        unique_terms = set()
        bbox = QgsRectangle()
        bbox.setMinimal()
        has_selection_bbox = False

        if use_selection:
            selection_count = source_layer.selectedFeatureCount()
            if selection_count > 0:
                feedback.pushInfo(
                    "{0} object(en) geselecteerd; codes en gebied worden "
                    "bepaald...".format(selection_count)
                )
                for feature in source_layer.selectedFeatures():
                    extracted_code = self._extract_search_term(feature[field_name])
                    if extracted_code:
                        unique_terms.add(extracted_code)

                    geom = feature.geometry()
                    if geom is not None and not geom.isEmpty():
                        if not has_selection_bbox:
                            bbox = geom.boundingBox()
                        else:
                            bbox.combineExtentWith(geom.boundingBox())
                        has_selection_bbox = True
            else:
                feedback.pushInfo(
                    "'Gebruik selectie' staat aan, maar er zijn geen objecten "
                    "geselecteerd."
                )

        if manual_search_string:
            manual_terms = (
                self._extract_search_term(term)
                for term in manual_search_string.split(",")
            )
            unique_terms.update(term for term in manual_terms if term)

        terms_list = sorted(unique_terms, key=lambda term: term.casefold())
        if not terms_list:
            raise QgsProcessingException(
                "Geen zoektermen gevonden. Selecteer minimaal één kabel uit de "
                "koppeluitkomst of vul handmatige zoektermen in."
            )

        feedback.pushInfo(
            "Zoeken naar {0} unieke projectcode(s): {1}".format(
                len(terms_list), ", ".join(terms_list[:20])
            )
        )

        # STAP 2: alleen de kabels in het ruimtelijke zoekgebied scannen.
        term_color_lookup = {
            term.casefold(): self.COLOR_MAP[index % len(self.COLOR_MAP)]
            for index, term in enumerate(terms_list)
        }

        sorted_terms = sorted(terms_list, key=len, reverse=True)
        main_regex = re.compile(
            "(" + "|".join(re.escape(term) for term in sorted_terms) + ")",
            re.IGNORECASE,
        )

        export_data = {}
        fields = self._dxf_fields(vary_colors)

        request = QgsFeatureRequest()
        if has_selection_bbox and use_selection:
            bbox.grow(search_radius)
            request.setFilterRect(bbox)
            feedback.pushInfo(
                "Zoekgebied: selectie-bounding-box + {0} meter buffer.".format(
                    search_radius
                )
            )
        else:
            feedback.pushInfo("Geen selectiegebied gevonden; hele laag wordt gescand.")

        requested_names = [field_name]
        if type_field_name:
            requested_names.append(type_field_name)
        if status_field_name:
            requested_names.append(status_field_name)
        request.setSubsetOfAttributes(
            list(dict.fromkeys(requested_names)), source_layer.fields()
        )

        scanned_count = 0
        matched_feature_count = 0
        for feature in source_layer.getFeatures(request):
            if feedback.isCanceled():
                break

            scanned_count += 1
            if scanned_count % 1000 == 0:
                feedback.pushInfo(
                    "{0} objecten gescand in zoekgebied...".format(scanned_count)
                )

            if only_matched and status_field_name:
                status = str(feature[status_field_name] or "").strip()
                if status.casefold() != "gekoppeld":
                    continue

            value = feature[field_name]
            if value is None:
                continue

            value_text = str(value).strip()
            if not value_text:
                continue

            match = main_regex.search(value_text)
            if not match:
                continue

            matched_feature_count += 1
            found_term = match.group(1)
            term_key = next(
                (
                    term
                    for term in terms_list
                    if term.casefold() == found_term.casefold()
                ),
                found_term,
            )

            hex_color = term_color_lookup.get(term_key.casefold(), "#FFFFFF")
            ogr_style_string = "PEN(c:{0})".format(hex_color) if vary_colors else ""

            clean_term = self._safe_name(term_key)
            file_key = "Gecombineerde_Export" if single_file else clean_term
            export_data.setdefault(file_key, {})

            type_value = feature[type_field_name] if type_field_name else None
            final_layer_name = self._layer_name(
                value_text,
                term_key,
                type_value,
                use_extended_names,
            )
            layer_data = export_data[file_key].setdefault(
                final_layer_name, {"geoms": [], "style": ogr_style_string}
            )

            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                layer_data["geoms"].append(QgsGeometry(geometry))

        feedback.pushInfo(
            "Scan gereed: {0} object(en) bekeken, {1} passend bij de "
            "projectcode(s).".format(scanned_count, matched_feature_count)
        )

        # STAP 3: wegschrijven naar DXF.
        total_layers = sum(len(layers) for layers in export_data.values())
        if total_layers == 0:
            feedback.pushInfo("Geen kabels gevonden om naar DXF te exporteren.")
            return {self.FEATURES_EXPORTED: 0, self.FILES_WRITTEN: 0}

        feedback.pushInfo(
            "Start export van {0} DXF-la(a)g(en)...".format(total_layers)
        )
        feedback.setProgress(50)
        processed_layers = 0
        exported_features = 0
        files_written = 0

        for file_name, layers in export_data.items():
            if feedback.isCanceled():
                break

            output_path = os.path.join(output_folder, file_name + ".dxf")
            writer = self._create_dxf_writer(
                output_path, fields, source_layer, context
            )
            files_written += 1

            for layer_name, data in layers.items():
                if feedback.isCanceled():
                    break

                processed_layers += 1
                feedback.setProgress(
                    50 + int((processed_layers / total_layers) * 50)
                )
                geoms = data["geoms"]
                if not geoms:
                    continue

                if merge_lines:
                    geometry = QgsGeometry.collectGeometry(geoms).mergeLines()
                    if geometry is not None and not geometry.isEmpty():
                        out_feature = QgsFeature(fields)
                        out_feature.setGeometry(geometry)
                        out_feature.setAttribute("Layer", layer_name)
                        if vary_colors:
                            out_feature.setAttribute("OGR_STYLE", data["style"])
                        if writer.addFeature(out_feature):
                            exported_features += 1
                else:
                    for geometry in geoms:
                        out_feature = QgsFeature(fields)
                        out_feature.setGeometry(geometry)
                        out_feature.setAttribute("Layer", layer_name)
                        if vary_colors:
                            out_feature.setAttribute("OGR_STYLE", data["style"])
                        if writer.addFeature(out_feature):
                            exported_features += 1

            del writer
            feedback.pushInfo("DXF opgeslagen: {0}".format(output_path))

        feedback.setProgress(100)
        feedback.pushInfo(
            "Klaar: {0} kabelobject(en) naar DXF geschreven.".format(
                exported_features
            )
        )
        return {
            self.FEATURES_EXPORTED: exported_features,
            self.FILES_WRITTEN: files_written,
        }
