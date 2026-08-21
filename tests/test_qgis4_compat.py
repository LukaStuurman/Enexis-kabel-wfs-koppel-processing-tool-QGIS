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

    def test_metadata_requires_qgis_42(self):
        text = (ROOT / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("qgisMinimumVersion=4.2", text)
        self.assertIn("version=0.7.0", text)

    def test_wfs_downloads_are_not_parallel(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertNotIn("ThreadPoolExecutor", text)
        self.assertNotIn("as_completed", text)
        self.assertNotIn("MAX_HTTP_WORKERS", text)

    def test_resource_safety_limits_exist(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("LABELS_PER_REQUEST = 5", text)
        self.assertIn("MAX_FEATURES_PER_BATCH = 500", text)
        self.assertIn("MAX_FEATURES_IN_EXTENT = 500", text)
        self.assertIn("MAX_RESPONSE_BYTES = 8 * 1024 * 1024", text)
        self.assertIn("response.read(limit + 1)", text)
        self.assertIn("gc.collect()", text)

    def test_only_minimal_wfs_attributes_are_parsed(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("QgsField(self.LABEL_FIELD, QMetaType.Type.QString)", text)
        self.assertNotIn("QgsJsonUtils.stringToFields", text)

    def test_extent_mode_is_extent_first(self):
        text = (ROOT / "algorithm_auto.py").read_text(encoding="utf-8")
        self.assertIn("def _extent_getfeature_url", text)
        self.assertIn('params["bbox"]', text)
        self.assertIn("Extent mode intentionally has NO CSV/label filter", text)
        self.assertIn("daarna lokaal vergelijken met de CSV", text)


if __name__ == "__main__":
    unittest.main()
