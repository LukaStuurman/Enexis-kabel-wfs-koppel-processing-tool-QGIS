# -*- coding: utf-8 -*-
"""Hot-path refinements for the v0.14 nationwide processor."""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from qgis.core import QgsProcessingException, QgsVectorLayer

from .nationwide import NationwideProcessor as BaseNationwideProcessor
from .wfs_index import WfsIndexBuilder, WfsIndexError, tile_bounds


class NationwideProcessor(BaseNationwideProcessor):
    def _records_from_gpkg(self, path, label_field):
        table_name, _ = self._gpkg_feature_table(path)
        if not table_name:
            return []
        uri = path + "|layername=" + table_name
        layer = QgsVectorLayer(uri, "enexis_wfs_tile", "ogr")
        if not layer.isValid():
            raise QgsProcessingException(
                "Gedownloade WFS-GeoPackage kon niet worden geopend."
            )
        field_lookup = {field.name().casefold(): field.name() for field in layer.fields()}
        actual_label = field_lookup.get(label_field.casefold(), label_field)
        actual_fid = field_lookup.get("fid")
        records = []
        for feature in layer.getFeatures():
            try:
                label_value = feature[actual_label]
            except Exception:
                label_value = None
            from .matching import normalize_label
            import sqlite3

            label = normalize_label(label_value)
            geometry = feature.geometry()
            if not label or geometry is None or geometry.isEmpty():
                continue
            geometry_wkb = bytes(geometry.asWkb())
            source_fid = ""
            if actual_fid:
                try:
                    source_fid = str(feature[actual_fid] or "")
                except Exception:
                    source_fid = ""
            source_key = self._source_key(source_fid, label, geometry_wkb)
            records.append(
                (
                    source_key,
                    str(source_fid or source_key),
                    label,
                    round(geometry.length(), 2),
                    sqlite3.Binary(geometry_wkb),
                )
            )
        del layer
        return records

    def _build_wfs_index(self, type_name, label_field):
        try:
            builder = WfsIndexBuilder(
                self.cache_folder,
                self.algorithm.WFS_URL,
                type_name,
                label_field,
                self.algorithm.GEOMETRY_FIELD,
                self.algorithm.RD_AUTHID,
            )
        except WfsIndexError as exc:
            raise QgsProcessingException(str(exc)) from exc

        download_format = self._benchmark_download_format(type_name, label_field)
        tiles = tile_bounds(self.NATIONWIDE_RD_BOUNDS, self.TILE_SIZE_M)
        total_tiles = len(tiles)
        completed_tiles = 0
        raw_features = 0
        unique_features = 0
        pending = {}
        tile_iter = iter(tiles)

        self.feedback.pushInfo(
            "Landelijke WFS-index: {0} RD-tegels van {1:.0f} km, maximaal {2} "
            "gelijktijdige downloads, formaat {3}.".format(
                total_tiles,
                self.TILE_SIZE_M / 1000,
                self.MAX_TILE_WORKERS,
                download_format,
            )
        )

        executor = ThreadPoolExecutor(max_workers=self.MAX_TILE_WORKERS)
        try:
            for _ in range(self.MAX_TILE_WORKERS):
                try:
                    tile = next(tile_iter)
                except StopIteration:
                    break
                pending[
                    executor.submit(
                        self._download_tile,
                        tile,
                        type_name,
                        label_field,
                        download_format,
                    )
                ] = tile

            while pending:
                self._cancel_if_requested(
                    "Landelijke WFS-indexbouw geannuleerd; de oude index blijft behouden."
                )
                done, _ = wait(tuple(pending.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    _, paths, returned = future.result()
                    raw_features += returned
                    try:
                        for path in paths:
                            records = (
                                self._records_from_gpkg(path, label_field)
                                if download_format == "geopkg"
                                else self._records_from_geojson(path, label_field)
                            )
                            unique_features += builder.insert_records(records)
                            del records
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                        builder.commit()
                    finally:
                        for path in paths:
                            try:
                                os.remove(path)
                            except OSError:
                                pass

                    completed_tiles += 1
                    self.feedback.setProgress(
                        5.0 + 45.0 * completed_tiles / max(1, total_tiles)
                    )
                    if completed_tiles % 10 == 0 or completed_tiles == total_tiles:
                        self.feedback.pushInfo(
                            "WFS-index: {0}/{1} tegels, {2} ruwe objecten gezien, "
                            "{3} unieke geldige kabels geïndexeerd.".format(
                                completed_tiles,
                                total_tiles,
                                raw_features,
                                unique_features,
                            )
                        )

                    try:
                        next_tile = next(tile_iter)
                    except StopIteration:
                        next_tile = None
                    if next_tile is not None:
                        pending[
                            executor.submit(
                                self._download_tile,
                                next_tile,
                                type_name,
                                label_field,
                                download_format,
                            )
                        ] = next_tile

            self._cancel_if_requested(
                "Landelijke WFS-indexbouw geannuleerd; de oude index blijft behouden."
            )
            return builder.finalize(
                download_format,
                raw_features,
                {
                    "tile_size_m": self.TILE_SIZE_M,
                    "tile_count": total_tiles,
                    "max_workers": self.MAX_TILE_WORKERS,
                },
            )
        except Exception:
            self.cancel_event.set()
            builder.abort()
            raise
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
