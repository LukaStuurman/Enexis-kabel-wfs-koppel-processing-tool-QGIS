import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Qgis42CompatibilityTests(unittest.TestCase):
    def test_no_qvariant_type_enums(self):
        for name in (
            "algorithm.py",
            "algorithm_auto.py",
            "algorithm_fast.py",
            "algorithm_source.py",
            "algorithm_dxf.py",
            "nationwide.py",
            "nationwide_fast.py",
            "shape_nationwide.py",
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("QVariant.String", text)
            self.assertNotIn("QVariant.Double", text)
            self.assertNotIn("QVariant.Int", text)

    def test_qgis4_native_wkb_enum(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("QgsWkbTypes.LineString", text)
        self.assertIn("Qgis.WkbType.MultiLineString", text)

    def test_metadata_requires_qgis_42_and_v0153(self):
        text = (ROOT / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("qgisMinimumVersion=4.2", text)
        self.assertIn("version=0.15.3", text)
        self.assertIn("SHAPE Noord", text)

    def test_provider_uses_source_selecting_v015_algorithm(self):
        provider = (ROOT / "provider.py").read_text(encoding="utf-8")
        self.assertIn("from .algorithm_source import KoppelWfsCsvAutoAlgorithm", provider)
        self.assertIn("self.addAlgorithm(KoppelWfsCsvAutoAlgorithm())", provider)
        self.assertIn("self.addAlgorithm(SplitNaarDXF())", provider)

    def test_cache_folder_remains_registered(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        init_part = text.split("def initAlgorithm", 1)[1].split("def _get_setting", 1)[0]
        self.assertIn("QgsProcessingParameterFolderDestination", init_part)
        self.assertIn("self.CACHE_FOLDER", init_part)

    def test_fast_algorithm_adds_refresh_and_matched_only_parameters(self):
        text = (ROOT / "algorithm_fast.py").read_text(encoding="utf-8")
        self.assertIn('TYPE_HINT = "asm_e_lv_map_cable"', text)
        self.assertIn('REFRESH_WFS_INDEX = "REFRESH_WFS_INDEX"', text)
        self.assertIn('ONLY_MATCHED_OUTPUT = "ONLY_MATCHED_OUTPUT"', text)
        self.assertIn("QgsProcessingParameterBoolean", text)
        self.assertIn("NationwideProcessor", text)

    def test_source_algorithm_offers_wfs_noord_zuid_and_both(self):
        text = (ROOT / "algorithm_source.py").read_text(encoding="utf-8")
        self.assertIn('NATIONWIDE_SOURCE = "NATIONWIDE_SOURCE"', text)
        self.assertIn('REFRESH_SHAPE_DOWNLOAD = "REFRESH_SHAPE_DOWNLOAD"', text)
        self.assertIn("WFS (online tegelindex)", text)
        self.assertIn("SHAPE Noord (automatisch downloaden)", text)
        self.assertIn("SHAPE Zuid (automatisch downloaden)", text)
        self.assertIn("SHAPE Noord + Zuid (automatisch downloaden)", text)
        self.assertIn("defaultValue=3", text)
        self.assertIn("ShapeNationwideProcessor", text)

    def test_shape_archive_uses_requested_imkl_folder_and_direct_s3_zip(self):
        text = (ROOT / "shape_archive.py").read_text(encoding="utf-8")
        self.assertIn(
            'TARGET_FOLDER = "imkl_elektriciteitskabel_e_lv_map_cable_ligging"',
            text,
        )
        self.assertIn("enxp433-opendata-publications.s3.eu-west-1.amazonaws.com", text)
        self.assertIn("Open_Asset_Data_Elektra.zip", text)
        self.assertNotIn('"https://c.spotler.com/ct/', text)
        self.assertIn("download_archive", text)
        self.assertIn("zipfile.is_zipfile", text)
        self.assertIn("discover_shape_files", text)
        self.assertIn('EXTRACTED_NAME = "shape_{0}"', text)
        self.assertIn('EXTRACT_STAGE_PREFIX = "sx_"', text)
        self.assertIn("build_target = build_root", text)
        self.assertIn('"noord"', text)
        self.assertIn('"zuid"', text)
        self.assertIn('(\".dbf\", \".shx\", \".prj\")', text)

    def test_shape_source_builds_persistent_geometry_index(self):
        text = (ROOT / "shape_nationwide.py").read_text(encoding="utf-8")
        self.assertIn("WfsIndexBuilder", text)
        self.assertIn("open_existing", text)
        self.assertIn("archive_edge_sha256", (ROOT / "shape_archive.py").read_text(encoding="utf-8"))
        self.assertIn("_geometry_source_key", text)
        self.assertIn("geometry.convertToMultiType()", text)
        self.assertIn("QgsCoordinateTransform", text)
        self.assertIn("WFS wordt voor deze run niet gebruikt", text)

    def test_reusable_csv_index_is_used_for_extent(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        index_text = (ROOT / "csv_index.py").read_text(encoding="utf-8")
        self.assertIn("from .csv_index import CsvIndexError, open_csv_index", text)
        self.assertIn("csv_index.rows_for_labels(found_labels)", text)
        self.assertIn('INDEX_PREFIX = "enexis_csv_index_"', index_text)
        self.assertIn("CREATE INDEX idx_csv_rows_label ON csv_rows(label)", index_text)

    def test_nationwide_uses_persistent_wfs_index(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        hot = (ROOT / "nationwide_fast.py").read_text(encoding="utf-8")
        index_text = (ROOT / "wfs_index.py").read_text(encoding="utf-8")
        self.assertIn("open_existing", active)
        self.assertIn("WfsIndexBuilder", hot)
        self.assertIn('INDEX_PREFIX = "enexis_wfs_index_"', index_text)
        self.assertIn("CREATE TABLE wfs_rows", index_text)
        self.assertIn("CREATE TABLE wfs_labels", index_text)
        self.assertIn("source_key TEXT PRIMARY KEY", index_text)

    def test_wfs_fallback_gets_missing_open_existing_symbol(self):
        text = (ROOT / "algorithm_source.py").read_text(encoding="utf-8")
        self.assertIn("_nationwide_fast.open_existing = _open_existing", text)

    def test_nationwide_uses_adaptive_bbox_tiles_and_bounded_parallelism(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        hot = (ROOT / "nationwide_fast.py").read_text(encoding="utf-8")
        self.assertIn("TILE_SIZE_M = 25000.0", active)
        self.assertIn("MAX_TILE_WORKERS = 2", active)
        self.assertIn('params["bbox"]', hot)
        self.assertIn("ThreadPoolExecutor(max_workers=self.MAX_TILE_WORKERS)", hot)
        self.assertIn("tile_bounds", hot)
        self.assertIn("def _split_tile", hot)
        self.assertIn("self.TILE_PAGE_SIZE + 1", hot)
        self.assertIn("pending_tiles.extend(self._split_tile(current))", hot)
        self.assertNotIn('params["startIndex"]', hot)
        self.assertNotIn('params["sortBy"]', hot)
        self.assertNotIn('"fid", self.algorithm.GEOMETRY_FIELD', hot)

    def test_nationwide_does_not_copy_full_csv_index(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        self.assertNotIn("shutil.copy2", active)
        self.assertIn("ATTACH DATABASE ? AS wfs", active)
        self.assertIn("CREATE TEMP TABLE matched_rows", active)
        self.assertIn("temp.matched_rows", active)

    def test_only_matched_fast_output_is_supported(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        self.assertIn("self.only_matched_output", active)
        self.assertIn("if self.only_matched_output and csv_idx is None", active)
        self.assertIn("niet-gekoppelde WFS-kabels", active)

    def test_geopackage_is_live_probed_and_benchmarked_for_wfs(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        self.assertIn('output_format="geopkg"', active)
        self.assertIn('b"SQLite format 3\\x00"', active)
        self.assertIn("Enexis WFS ondersteunt GeoPackage", active)

    def test_cancel_never_silently_finishes_partial_index(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        hot = (ROOT / "nationwide_fast.py").read_text(encoding="utf-8")
        shape = (ROOT / "shape_nationwide.py").read_text(encoding="utf-8")
        self.assertIn("cancel_event", active)
        self.assertIn("oude index blijft behouden", hot)
        self.assertIn("builder.abort()", hot)
        self.assertIn("builder.abort()", shape)

    def test_extent_scan_is_label_only(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _fetch_extent_labels", text)
        self.assertIn('params["propertyName"] = label_field', text)
        self.assertIn("MAX_EXTENT_LABEL_FEATURES = 10000", text)
        self.assertIn("MAX_LABEL_SCAN_BYTES = 4 * 1024 * 1024", text)

    def test_extent_geometry_only_for_common_labels(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("common_labels = sorted(found_labels & set(csv_groups.keys()))", text)
        self.assertIn("LABELS_PER_GEOMETRY_REQUEST = 10", text)
        self.assertIn("def _extent_label_filter", text)
        self.assertIn("BBOX({0},{1},{2},{3},{4},'{5}')", text)
        self.assertIn('GEOMETRY_FIELD = "geografischeligging"', text)

    def test_geometry_parsed_with_qgis(self):
        text = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        self.assertIn("QgsJsonUtils.geometryFromGeoJson", text)
        self.assertIn("QgsVectorLayer", text)

    def test_dxf_algorithm_is_registered(self):
        provider = (ROOT / "provider.py").read_text(encoding="utf-8")
        self.assertIn("from .algorithm_dxf import SplitNaarDXF", provider)
        self.assertIn("self.addAlgorithm(SplitNaarDXF())", provider)

    def test_dxf_has_constant_memory_nationwide_mode(self):
        text = (ROOT / "algorithm_dxf.py").read_text(encoding="utf-8")
        self.assertIn("def _stream_whole_country", text)
        self.assertIn("request.setSubsetOfAttributes", text)
        self.assertIn('"Nederland_Kabels_{0:04d}.dxf"', text)
        self.assertIn("features_in_file >= features_per_file", text)
        self.assertIn('status.casefold() != "gekoppeld"', text)
        self.assertIn("zlib.crc32", text)


if __name__ == "__main__":
    unittest.main()
