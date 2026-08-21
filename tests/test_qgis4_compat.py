import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Qgis42CompatibilityTests(unittest.TestCase):
    def test_no_qvariant_type_enums(self):
        for name in (
            "algorithm.py",
            "algorithm_auto.py",
            "algorithm_fast.py",
            "algorithm_dxf.py",
            "nationwide.py",
            "nationwide_fast.py",
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("QVariant.String", text)
            self.assertNotIn("QVariant.Double", text)
            self.assertNotIn("QVariant.Int", text)

    def test_qgis4_native_wkb_enum(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("QgsWkbTypes.LineString", text)
        self.assertIn("Qgis.WkbType.MultiLineString", text)

    def test_metadata_requires_qgis_42_and_v014(self):
        text = (ROOT / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("qgisMinimumVersion=4.2", text)
        self.assertIn("version=0.14.0", text)

    def test_provider_uses_fast_v014_algorithm(self):
        provider = (ROOT / "provider.py").read_text(encoding="utf-8")
        self.assertIn("from .algorithm_fast import KoppelWfsCsvAutoAlgorithm", provider)
        self.assertIn("self.addAlgorithm(KoppelWfsCsvAutoAlgorithm())", provider)
        self.assertIn("self.addAlgorithm(SplitNaarDXF())", provider)

    def test_cache_folder_remains_registered(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        init_part = text.split("def initAlgorithm", 1)[1].split("def _get_setting", 1)[0]
        self.assertIn("QgsProcessingParameterFolderDestination", init_part)
        self.assertIn("self.CACHE_FOLDER", init_part)

    def test_fast_algorithm_adds_refresh_and_matched_only_parameters(self):
        text = (ROOT / "algorithm_fast.py").read_text(encoding="utf-8")
        self.assertIn('REFRESH_WFS_INDEX = "REFRESH_WFS_INDEX"', text)
        self.assertIn('ONLY_MATCHED_OUTPUT = "ONLY_MATCHED_OUTPUT"', text)
        self.assertIn("QgsProcessingParameterBoolean", text)
        self.assertIn("defaultValue=False", text)
        self.assertIn("defaultValue=True", text)
        self.assertIn("NationwideProcessor", text)

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

    def test_nationwide_uses_bbox_tiles_and_bounded_parallelism(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        hot = (ROOT / "nationwide_fast.py").read_text(encoding="utf-8")
        self.assertIn("TILE_SIZE_M = 25000.0", active)
        self.assertIn("MAX_TILE_WORKERS = 2", active)
        self.assertIn('params["bbox"]', active)
        self.assertIn("ThreadPoolExecutor(max_workers=self.MAX_TILE_WORKERS)", hot)
        self.assertIn("tile_bounds", hot)

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

    def test_geopackage_is_live_probed_and_benchmarked(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        self.assertIn('output_format="geopkg"', active)
        self.assertIn('b"SQLite format 3\\x00"', active)
        self.assertIn("Enexis WFS ondersteunt GeoPackage", active)
        self.assertIn("GeoPackage is beschikbaar maar", active)

    def test_cancel_never_silently_finishes_partial_index(self):
        active = (ROOT / "nationwide.py").read_text(encoding="utf-8")
        hot = (ROOT / "nationwide_fast.py").read_text(encoding="utf-8")
        self.assertIn("cancel_event", active)
        self.assertIn("oude index blijft behouden", hot)
        self.assertIn("builder.abort()", hot)

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
