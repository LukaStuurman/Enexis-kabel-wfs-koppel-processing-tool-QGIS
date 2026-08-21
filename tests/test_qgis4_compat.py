import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Qgis42CompatibilityTests(unittest.TestCase):
    def test_no_qvariant_type_enums(self):
        for name in ("algorithm.py", "algorithm_auto.py", "algorithm_dxf.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("QVariant.String", text)
            self.assertNotIn("QVariant.Double", text)
            self.assertNotIn("QVariant.Int", text)

    def test_qgis4_native_wkb_enum(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("QgsWkbTypes.LineString", text)
        self.assertIn("Qgis.WkbType.MultiLineString", text)

    def test_metadata_requires_qgis_42_and_v013(self):
        text = (ROOT / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("qgisMinimumVersion=4.2", text)
        self.assertIn("version=0.13.0", text)

    def test_cache_folder_is_registered_in_init(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        init_part = text.split("def initAlgorithm", 1)[1].split("def _get_setting", 1)[0]
        process_part = text.split("def processAlgorithm", 1)[1]
        self.assertIn("QgsProcessingParameterFolderDestination", init_part)
        self.assertIn("self.CACHE_FOLDER", init_part)
        self.assertNotIn("self.addParameter(", process_part)

    def test_reusable_csv_index_is_used(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        index_text = (ROOT / "csv_index.py").read_text(encoding="utf-8")
        self.assertIn("from .csv_index import CsvIndexError, open_csv_index", text)
        self.assertIn("csv_index.rows_for_labels(found_labels)", text)
        self.assertIn("INDEX_PREFIX = \"enexis_csv_index_\"", index_text)
        self.assertIn("source_edge_sha256", index_text)
        self.assertIn("CREATE INDEX idx_csv_rows_label ON csv_rows(label)", index_text)

    def test_persistent_index_has_no_run_specific_match_state(self):
        index_text = (ROOT / "csv_index.py").read_text(encoding="utf-8")
        self.assertNotIn("matched INTEGER", index_text)
        self.assertNotIn("wfs_found INTEGER", index_text)
        active = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("ALTER TABLE csv_rows ADD COLUMN matched", active)
        self.assertIn("ALTER TABLE csv_rows ADD COLUMN wfs_found", active)
        self.assertIn("enexis_landelijk_run_", active)

    def test_cancel_never_silently_finishes_partial_nationwide_run(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _cancel_if_requested", text)
        self.assertNotIn("while not feedback.isCanceled()", text)
        self.assertIn("gedeeltelijk resultaat", text)
        self.assertIn("gedeeltelijke uitvoer", text)

    def test_nationwide_temporary_outputs_are_refused(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _require_nationwide_disk_outputs", text)
        self.assertIn("TEMPORARY_OUTPUT", text)
        self.assertIn('text.lower().startswith("memory:")', text)
        self.assertIn("GeoPackage (.gpkg)", text)

    def test_nationwide_wfs_is_paged_once_and_joined_locally(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("NATIONWIDE_WFS_PAGE_SIZE = 10000", text)
        self.assertIn("MAX_NATIONWIDE_PAGE_BYTES = 64 * 1024 * 1024", text)
        self.assertIn('params["startIndex"] = str(start_index)', text)
        self.assertIn('params["sortBy"] = "fid A"', text)
        self.assertIn("NATIONWIDE_MATCH_LABEL_BATCH = 50", text)

    def test_wfs_transient_errors_are_retried(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("HTTP_RETRIES = 3", text)
        self.assertIn("retryable = exc.code == 429", text)
        self.assertIn("time.sleep(2**attempt)", text)

    def test_wfs_downloads_are_not_parallel(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("ThreadPoolExecutor", text)
        self.assertNotIn("as_completed", text)
        self.assertNotIn("MAX_HTTP_WORKERS", text)

    def test_extent_scan_is_label_only(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _fetch_extent_labels", text)
        self.assertIn('params["propertyName"] = label_field', text)
        self.assertIn("MAX_EXTENT_LABEL_FEATURES = 10000", text)
        self.assertIn("MAX_LABEL_SCAN_BYTES = 4 * 1024 * 1024", text)

    def test_geometry_only_for_common_labels(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("common_labels = sorted(found_labels & set(csv_groups.keys()))", text)
        self.assertIn("LABELS_PER_GEOMETRY_REQUEST = 10", text)
        self.assertIn("def _extent_label_filter", text)
        self.assertIn("BBOX({0},{1},{2},{3},{4},'{5}')", text)
        self.assertIn('GEOMETRY_FIELD = "geografischeligging"', text)

    def test_geometry_parsed_directly_from_geojson(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("QgsJsonUtils.geometryFromGeoJson", text)
        self.assertNotIn("QgsJsonUtils.stringToFeatureList", text)
        self.assertNotIn("QgsJsonUtils.stringToFields", text)

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
