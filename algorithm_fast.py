# -*- coding: utf-8 -*-
"""v0.14 processing algorithm with fast nationwide persistent indexes."""

from qgis.core import QgsProcessingParameterBoolean

from .algorithm_auto import KoppelWfsCsvAutoAlgorithm as BaseKoppelWfsCsvAutoAlgorithm
from .nationwide import NationwideProcessor


class KoppelWfsCsvAutoAlgorithm(BaseKoppelWfsCsvAutoAlgorithm):
    REFRESH_WFS_INDEX = "REFRESH_WFS_INDEX"
    ONLY_MATCHED_OUTPUT = "ONLY_MATCHED_OUTPUT"

    def createInstance(self):
        return KoppelWfsCsvAutoAlgorithm()

    def displayName(self):
        return self.tr(
            "Koppel Enexis WFS-kabels aan CSV (snelle extent of landelijke index)"
        )

    def shortHelpString(self):
        return self.tr(
            "Met een extent gebruikt de tool de lichte labelscan en de herbruikbare "
            "CSV-index. Zonder extent gebruikt v0.14 twee permanente lokale indexes: "
            "de CSV-index en een landelijke WFS-index. De WFS-index wordt eenmalig "
            "met 25 km RD-tegels opgebouwd, met maximaal twee gelijktijdige downloads. "
            "Daarna is een nieuwe landelijke koppeling volledig lokaal, behalve wanneer "
            "'Vernieuw landelijke WFS-index' is aangevinkt. De plugin test bij een "
            "indexbouw ook of de Enexis-server GeoPackage-output aanbiedt en kiest dit "
            "alleen als een live benchmark voordeel laat zien. Voor maximale snelheid "
            "kan alleen GEKOPPELD-uitvoer worden geschreven."
        )

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.REFRESH_WFS_INDEX,
                self.tr("Vernieuw landelijke WFS-index vanaf Enexis"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ONLY_MATCHED_OUTPUT,
                self.tr(
                    "Landelijk: alleen GEKOPPELD schrijven (snelste; geen unmatched CSV)"
                ),
                defaultValue=True,
            )
        )

    def _process_nationwide(
        self,
        parameters,
        context,
        feedback,
        csv_path,
        source_crs,
        cache_folder,
        total_started,
    ):
        refresh_wfs_index = self.parameterAsBool(
            parameters, self.REFRESH_WFS_INDEX, context
        )
        only_matched_output = self.parameterAsBool(
            parameters, self.ONLY_MATCHED_OUTPUT, context
        )
        return NationwideProcessor(
            algorithm=self,
            parameters=parameters,
            context=context,
            feedback=feedback,
            csv_path=csv_path,
            source_crs=source_crs,
            cache_folder=cache_folder,
            total_started=total_started,
            refresh_wfs_index=refresh_wfs_index,
            only_matched_output=only_matched_output,
        ).run()
