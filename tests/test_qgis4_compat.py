import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Qgis42CompatibilityTests(unittest.TestCase):
    def test_no_qvariant_type_enums(self):
        for name in ("algorithm.py", "algorithm_auto.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("QVariant.String", text)
            self.assertNotIn("QVariant.Double", text)
            self.assertNotIn("QVariant.Int", text)

    def test_no_legacy_wkb_enum_in_active_algorithm(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("QgsWkbTypes.LineString", text)
        self.assertIn("Qgis.WkbType.LineString", text)

    def test_modern_qgis42_field_types(self):
        text = (ROOT / "algorithm.py").read_text(encoding="utf-8")
        self.assertIn("QMetaType.Type.QString", text)
        self.assertIn("QMetaType.Type.Double", text)
        self.assertIn("QMetaType.Type.Int", text)

    def test_metadata_requires_qgis_42_and_v08(self):
        text = (ROOT / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("qgisMinimumVersion=4.2", text)
        self.assertIn("version=0.8.0", text)

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
        self.assertIn("common_labels = sorted(extent_labels & set(csv_groups.keys()))", text)
        self.assertIn("def _fetch_geometry_records", text)
        self.assertIn("LABELS_PER_GEOMETRY_REQUEST = 10", text)
        self.assertIn("cql_filter", text)

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
