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

    def test_no_legacy_wkb_enum_in_active_algorithm(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("QgsWkbTypes.LineString", text)
        self.assertIn("Qgis.WkbType.MultiLineString", text)

    def test_modern_qgis42_field_types(self):
        text = (ROOT / "algorithm.py").read_text(encoding="utf-8")
        self.assertIn("QMetaType.Type.QString", text)
        self.assertIn("QMetaType.Type.Double", text)
        self.assertIn("QMetaType.Type.Int", text)

    def test_metadata_requires_qgis_42_and_v012(self):
        text = (ROOT / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("qgisMinimumVersion=4.2", text)
        self.assertIn("version=0.12.0", text)

    def test_dxf_algorithm_is_registered(self):
        provider = (ROOT / "provider.py").read_text(encoding="utf-8")
        self.assertIn("from .algorithm_dxf import SplitNaarDXF", provider)
        self.assertIn("self.addAlgorithm(SplitNaarDXF())", provider)

    def test_dxf_uses_coupling_output_fields(self):
        text = (ROOT / "algorithm_dxf.py").read_text(encoding="utf-8")
        self.assertIn('"wfs_label_norm"', text)
        self.assertIn('"csv_Kabel Subgroep"', text)
        self.assertIn('"csv_Type"', text)
        self.assertIn('text.split("-", 1)[0].strip()', text)
        self.assertIn('r"^\\s*Kabelgroup\\s*:\\s*"', text)

    def test_dxf_uses_qgis4_native_enums(self):
        text = (ROOT / "algorithm_dxf.py").read_text(encoding="utf-8")
        self.assertIn("QMetaType.Type.QString", text)
        self.assertIn("Qgis.ProcessingNumberParameterType.Integer", text)

    def test_dxf_has_constant_memory_nationwide_mode(self):
        text = (ROOT / "algorithm_dxf.py").read_text(encoding="utf-8")
        self.assertIn("def _stream_whole_country", text)
        self.assertIn("request.setSubsetOfAttributes", text)
        self.assertIn('"Nederland_Kabels_{0:04d}.dxf"', text)
        self.assertIn("features_in_file >= features_per_file", text)
        self.assertIn('status.casefold() != "gekoppeld"', text)
        self.assertIn("zlib.crc32", text)

    def test_nationwide_matching_is_disk_backed(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _build_nationwide_cache", text)
        self.assertIn("CREATE TABLE csv_rows", text)
        self.assertIn("CREATE TABLE wfs_rows", text)
        self.assertIn("values_json TEXT NOT NULL", text)
        self.assertIn("def _process_nationwide", text)
        self.assertIn("return self._process_nationwide", text)
        self.assertIn("os.remove(cache_path)", text)

    def test_nationwide_wfs_is_paged_once_and_joined_locally(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("NATIONWIDE_WFS_PAGE_SIZE = 10000", text)
        self.assertIn("MAX_NATIONWIDE_PAGE_BYTES = 64 * 1024 * 1024", text)
        self.assertIn('params["startIndex"] = str(start_index)', text)
        self.assertIn('params["sortBy"] = "fid A"', text)
        self.assertIn("def _cached_wfs_records_for_labels", text)
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
        self.assertIn("def _fetch_geometry_records", text)
        self.assertIn("LABELS_PER_GEOMETRY_REQUEST = 10", text)
        self.assertIn("cql_filter", text)
        self.assertIn("def _extent_label_filter", text)
        self.assertIn("BBOX({0},{1},{2},{3},{4},'{5}')", text)
        self.assertIn('GEOMETRY_FIELD = "geografischeligging"', text)

    def test_extent_filters_nationwide_csv_before_copying_rows(self):
        helper = (ROOT / "algorithm.py").read_text(encoding="utf-8")
        active = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _read_csv(self, path, allowed_labels=None)", helper)
        self.assertIn("if allowed is not None and label not in allowed:", helper)
        self.assertIn("csv_allowed_labels = found_labels if extent is not None else None", active)
        self.assertIn("allowed_labels=csv_allowed_labels", active)
        self.assertIn("Rijen buiten de extent worden", active)

    def test_phase_timings_are_reported(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("Timing: schema", text)
        self.assertIn("labelscan", text)
        self.assertIn("WFS-geometrie", text)

    def test_geometry_parsed_directly_from_geojson(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("QgsJsonUtils.geometryFromGeoJson", text)
        self.assertNotIn("QgsJsonUtils.stringToFeatureList", text)
        self.assertNotIn("QgsJsonUtils.stringToFields", text)

    def test_label_field_is_detected_not_hardcoded(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _detect_label_field", text)
        self.assertIn('text.startswith("kabelgroup:")', text)
        self.assertIn("WFS-labelveld gedetecteerd", text)

    def test_zero_match_diagnostics(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("Geen exacte labelovereenkomst", text)
        self.assertIn("WFS-voorbeeld", text)
        self.assertIn("CSV-voorbeeld", text)

    def test_resource_limits_and_gc(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("response.read(limit + 1)", text)
        self.assertIn("MAX_GEOMETRY_RESPONSE_BYTES = 8 * 1024 * 1024", text)
        self.assertIn("gc.collect()", text)


if __name__ == "__main__":
    unittest.main()
