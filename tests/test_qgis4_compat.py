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


if __name__ == "__main__":
    unittest.main()
