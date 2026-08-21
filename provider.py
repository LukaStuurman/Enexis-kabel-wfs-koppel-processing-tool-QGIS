# -*- coding: utf-8 -*-

from qgis.core import QgsProcessingProvider

from .algorithm_fast import KoppelWfsCsvAutoAlgorithm
from .algorithm_dxf import SplitNaarDXF


class EnexisKabelProvider(QgsProcessingProvider):
    def id(self):
        return "enexiskabel"

    def name(self):
        return "Enexis"

    def longName(self):
        return "Enexis kabelkoppeling"

    def loadAlgorithms(self):
        self.addAlgorithm(KoppelWfsCsvAutoAlgorithm())
        self.addAlgorithm(SplitNaarDXF())
