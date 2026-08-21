# -*- coding: utf-8 -*-
"""Nationwide matching backed by the automatically downloaded Enexis SHAPE ZIP."""

from __future__ import annotations

import hashlib
import os
import sqlite3

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProcessingException,
    QgsVectorLayer,
)

from .matching import normalize_label
from .nationwide_fast import NationwideProcessor
from .shape_archive import (
    SHAPE_DOWNLOAD_URL,
    ShapeArchiveError,
    ensure_shape_archive,
)
from .wfs_index import WfsIndexBuilder, WfsIndexError, open_existing


class ShapeNationwideProcessor(NationwideProcessor):
    """Reuse the local matching engine while sourcing geometries from SHAPE."""

    SOURCE_GEOMETRY_FIELD = "shape_geometry"
    INDEX_BATCH_SIZE = 5000
    LABEL_SAMPLE_SIZE = 500

    def __init__(self, *args, region_mode="beide", refresh_shape=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.region_mode = str(region_mode)
        self.refresh_shape = bool(refresh_shape)

    def run(self):
        # Base run resolves a WFS schema before opening its source index. SHAPE mode
        # deliberately avoids any WFS/network dependency here; the actual label
        # field is detected from the DBF while opening/building the SHAPE index.
        original_resolver = self.algorithm._resolve_type_and_label_field
        self.algorithm._resolve_type_and_label_field = (
            lambda extent, feedback: ("shape:" + self.region_mode, "shape:auto")
        )
        try:
            self.feedback.pushInfo(
                "Landelijke bron: automatische Enexis SHAPE-download ({0}). WFS wordt voor deze run niet gebruikt.".format(
                    self.region_mode
                )
            )
            return super().run()
        finally:
            self.algorithm._resolve_type_and_label_field = original_resolver

    def _selected_paths(self, shape_files):
        if self.region_mode == "noord":
            return [("noord", shape_files["noord"])]
        if self.region_mode == "zuid":
            return [("zuid", shape_files["zuid"])]
        if self.region_mode == "beide":
            return [("noord", shape_files["noord"]), ("zuid", shape_files["zuid"])]
        raise QgsProcessingException("Onbekende SHAPE-regiokeuze: " + self.region_mode)

    @staticmethod
    def _name_score(field_name):
        text = str(field_name or "").casefold()
        compact = text.replace("_", "").replace("-", "").replace(" ", "")
        if text == "label":
            return 100
        if "label" in text:
            return 90
        if compact in ("kabelgroep", "kabelgroup", "cablegroup"):
            return 85
        if "kabelgro" in compact or "kabelgrp" in compact:
            return 80
        if "cablegro" in compact:
            return 75
        return 0

    def _detect_shape_label_field(self, layer):
        fields = [field.name() for field in layer.fields()]
        scored = sorted(
            ((self._name_score(name), name) for name in fields), reverse=True
        )
        if scored and scored[0][0] >= 80:
            return scored[0][1]

        prefix_hits = {name: 0 for name in fields}
        request = QgsFeatureRequest()
        request.setLimit(self.LABEL_SAMPLE_SIZE)
        for feature in layer.getFeatures(request):
            for name in fields:
                try:
                    value = str(feature[name] or "").strip().casefold()
                except Exception:
                    value = ""
                if value.startswith("kabelgroup:") or value.startswith("kabelgroep:"):
                    prefix_hits[name] += 1
        ranked = sorted(
            ((hits, name) for name, hits in prefix_hits.items() if hits), reverse=True
        )
        if ranked:
            return ranked[0][1]
        raise QgsProcessingException(
            "Geen kabelgroep/labelveld gevonden in SHAPE '{0}'. Velden: {1}".format(
                layer.name(), ", ".join(fields)
            )
        )

    def _open_layer(self, path, region):
        layer = QgsVectorLayer(path, "Enexis kabels " + region, "ogr")
        if not layer.isValid():
            raise QgsProcessingException(
                "Enexis SHAPE kon niet worden geopend: " + path
            )
        if layer.crs() is None or not layer.crs().isValid():
            raise QgsProcessingException(
                "Enexis SHAPE heeft geen geldig CRS: " + os.path.basename(path)
            )
        return layer

    def _shape_layout(self, selected_paths):
        layout = []
        for region, path in selected_paths:
            layer = self._open_layer(path, region)
            try:
                label_field = self._detect_shape_label_field(layer)
                layout.append((region, path, label_field, layer.crs().authid()))
            finally:
                del layer
        signature = "|".join(
            "{0}:{1}".format(region, label_field) for region, _, label_field, _ in layout
        )
        return layout, signature

    @staticmethod
    def _fingerprint_matches(index, fingerprint):
        return all(index.meta.get(key) == value for key, value in fingerprint.items())

    @staticmethod
    def _geometry_source_key(label, geometry_wkb):
        digest = hashlib.sha1()
        digest.update(str(label).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(geometry_wkb)
        return "shape:" + digest.hexdigest()

    def _transform_for_layer(self, layer):
        if layer.crs() == self.source_crs:
            return None
        try:
            return QgsCoordinateTransform(
                layer.crs(), self.source_crs, self.context.transformContext()
            )
        except TypeError:
            return QgsCoordinateTransform(layer.crs(), self.source_crs)

    def _build_shape_index(self, layout, label_signature, fingerprint):
        source_type = "shape:" + self.region_mode
        try:
            builder = WfsIndexBuilder(
                self.cache_folder,
                SHAPE_DOWNLOAD_URL,
                source_type,
                label_signature,
                self.SOURCE_GEOMETRY_FIELD,
                self.algorithm.RD_AUTHID,
            )
        except WfsIndexError as exc:
            raise QgsProcessingException(str(exc)) from exc

        total_expected = 0
        layers = []
        try:
            for region, path, label_field, _ in layout:
                layer = self._open_layer(path, region)
                layers.append((region, path, label_field, layer))
                count = layer.featureCount()
                if count > 0:
                    total_expected += int(count)

            raw_count = 0
            inserted_count = 0
            batch = []
            for region, path, label_field, layer in layers:
                transform = self._transform_for_layer(layer)
                self.feedback.pushInfo(
                    "SHAPE-index leest {0}: {1} features, labelveld '{2}', CRS {3}.".format(
                        os.path.basename(path), layer.featureCount(), label_field, layer.crs().authid()
                    )
                )
                for feature in layer.getFeatures():
                    self._cancel_if_requested(
                        "SHAPE-indexbouw geannuleerd; bestaande bronindex blijft behouden."
                    )
                    raw_count += 1
                    try:
                        label_value = feature[label_field]
                    except Exception:
                        label_value = None
                    label = normalize_label(label_value)
                    geometry = QgsGeometry(feature.geometry())
                    if not label or geometry is None or geometry.isEmpty():
                        continue
                    if transform is not None:
                        try:
                            geometry.transform(transform)
                        except Exception as exc:
                            raise QgsProcessingException(
                                "SHAPE-geometrie kon niet naar EPSG:28992 worden getransformeerd."
                            ) from exc
                    if not geometry.isMultipart():
                        geometry.convertToMultiType()
                    geometry_wkb = bytes(geometry.asWkb())
                    if not geometry_wkb:
                        continue
                    source_key = self._geometry_source_key(label, geometry_wkb)
                    source_fid = "{0}:{1}".format(region, feature.id())
                    batch.append(
                        (
                            source_key,
                            source_fid,
                            label,
                            round(geometry.length(), 2),
                            sqlite3.Binary(geometry_wkb),
                        )
                    )
                    if len(batch) >= self.INDEX_BATCH_SIZE:
                        inserted_count += builder.insert_records(batch)
                        builder.commit()
                        batch = []
                    if raw_count % 25000 == 0:
                        self.feedback.setProgress(
                            5.0 + 45.0 * raw_count / max(1, total_expected)
                        )
                        self.feedback.pushInfo(
                            "SHAPE-index: {0}/{1} features gelezen; {2} unieke kabels ingevoegd.".format(
                                raw_count, total_expected or "?", inserted_count
                            )
                        )

            if batch:
                inserted_count += builder.insert_records(batch)
                builder.commit()

            self._cancel_if_requested(
                "SHAPE-indexbouw geannuleerd; bestaande bronindex blijft behouden."
            )
            extra = dict(fingerprint)
            extra.update(
                {
                    "source_kind": "shape_zip",
                    "region_mode": self.region_mode,
                    "shape_files": ";".join(os.path.basename(item[1]) for item in layout),
                }
            )
            index = builder.finalize("shape-zip", raw_count, extra)
            self.feedback.pushInfo(
                "SHAPE-kabelindex gereed: {0} bronfeatures gelezen; {1} unieke geldige kabels opgeslagen.".format(
                    raw_count, index.meta.get("feature_count", inserted_count)
                )
            )
            return index
        except Exception:
            builder.abort()
            raise
        finally:
            for _, _, _, layer in layers:
                del layer

    def _open_or_build_wfs_index(self, type_name, label_field):
        del type_name, label_field
        try:
            archive = ensure_shape_archive(
                self.cache_folder,
                refresh=self.refresh_shape,
                feedback=self.feedback,
            )
        except ShapeArchiveError as exc:
            raise QgsProcessingException(str(exc)) from exc

        selected_paths = self._selected_paths(archive["shape_files"])
        layout, label_signature = self._shape_layout(selected_paths)
        source_type = "shape:" + self.region_mode
        force_rebuild = bool(self.refresh_shape or archive["downloaded"])

        if not force_rebuild:
            existing = open_existing(
                self.cache_folder,
                SHAPE_DOWNLOAD_URL,
                source_type,
                label_signature,
                self.SOURCE_GEOMETRY_FIELD,
                self.algorithm.RD_AUTHID,
            )
            if existing is not None:
                if self._fingerprint_matches(existing, archive["fingerprint"]):
                    self.feedback.pushInfo(
                        "Permanente SHAPE-kabelindex hergebruikt: {0} kabels, regio {1}.".format(
                            existing.meta.get("feature_count", "?"), self.region_mode
                        )
                    )
                    return existing
                existing.close()
                self.feedback.pushInfo(
                    "Lokale SHAPE ZIP is gewijzigd; kabelindex wordt opnieuw opgebouwd."
                )

        return self._build_shape_index(
            layout, label_signature, archive["fingerprint"]
        )
