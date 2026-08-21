# -*- coding: utf-8 -*-
"""Processing algorithm with WFS or automatic Noord/Zuid SHAPE source choice."""

from qgis.core import QgsProcessingParameterBoolean, QgsProcessingParameterEnum

from . import nationwide_fast as _nationwide_fast
from .algorithm_fast import KoppelWfsCsvAutoAlgorithm as FastKoppelAlgorithm
from .shape_nationwide import ShapeNationwideProcessor
from .wfs_index import open_existing as _open_existing

# v0.14's fast WFS module uses open_existing in its hot path. Export it into
# that module namespace as well, so the WFS fallback is executable in real runs.
_nationwide_fast.open_existing = _open_existing


class KoppelWfsCsvAutoAlgorithm(FastKoppelAlgorithm):
    NATIONWIDE_SOURCE = "NATIONWIDE_SOURCE"
    REFRESH_SHAPE_DOWNLOAD = "REFRESH_SHAPE_DOWNLOAD"
    SOURCE_OPTIONS = (
        "WFS (online tegelindex)",
        "SHAPE Noord (automatisch downloaden)",
        "SHAPE Zuid (automatisch downloaden)",
        "SHAPE Noord + Zuid (automatisch downloaden)",
    )

    def createInstance(self):
        return KoppelWfsCsvAutoAlgorithm()

    def displayName(self):
        return self.tr(
            "Koppel Enexis kabels aan CSV (extent of landelijke WFS/SHAPE-bron)"
        )

    def shortHelpString(self):
        return self.tr(
            "Voor een extent blijft de snelle WFS-labelroute actief. Zonder extent "
            "kan de landelijke bron worden gekozen: WFS, SHAPE Noord, SHAPE Zuid of "
            "SHAPE Noord + Zuid. De SHAPE-opties downloaden automatisch de Enexis ZIP "
            "via de ingestelde downloadlink, pakken alleen de map "
            "'imkl_elektriciteitskabel_e_lv_map_cable_ligging' uit en zoeken daarin "
            "exact één Noord- en één Zuid-SHAPE. De gekozen laag/lagen worden naar een "
            "permanente lokale kabelindex geconverteerd en daarna volledig lokaal aan "
            "de CSV-index gekoppeld. Noord + Zuid is standaard voor heel Nederland."
        )

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self.addParameter(
            QgsProcessingParameterEnum(
                self.NATIONWIDE_SOURCE,
                self.tr("Landelijke kabelbron"),
                options=list(self.SOURCE_OPTIONS),
                defaultValue=3,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.REFRESH_SHAPE_DOWNLOAD,
                self.tr("SHAPE ZIP opnieuw downloaden en SHAPE-index vernieuwen"),
                defaultValue=False,
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
        source_choice = self.parameterAsEnum(
            parameters, self.NATIONWIDE_SOURCE, context
        )
        if source_choice == 0:
            return super()._process_nationwide(
                parameters,
                context,
                feedback,
                csv_path,
                source_crs,
                cache_folder,
                total_started,
            )

        region_mode = {1: "noord", 2: "zuid", 3: "beide"}.get(source_choice)
        if region_mode is None:
            region_mode = "beide"
        refresh_shape = self.parameterAsBool(
            parameters, self.REFRESH_SHAPE_DOWNLOAD, context
        )
        only_matched_output = self.parameterAsBool(
            parameters, self.ONLY_MATCHED_OUTPUT, context
        )
        if self.parameterAsBool(parameters, self.REFRESH_WFS_INDEX, context):
            feedback.pushInfo(
                "'Vernieuw landelijke WFS-index' is genegeerd omdat een SHAPE-bron is gekozen. "
                "Gebruik 'SHAPE ZIP opnieuw downloaden' om de SHAPE-bron te verversen."
            )
        return ShapeNationwideProcessor(
            algorithm=self,
            parameters=parameters,
            context=context,
            feedback=feedback,
            csv_path=csv_path,
            source_crs=source_crs,
            cache_folder=cache_folder,
            total_started=total_started,
            refresh_wfs_index=False,
            only_matched_output=only_matched_output,
            region_mode=region_mode,
            refresh_shape=refresh_shape,
        ).run()
