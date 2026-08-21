import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_NAME = "enexiskabel_wfs_testpkg"

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package

spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME + ".wfs_index", ROOT / "wfs_index.py"
)
wfs_index = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wfs_index
spec.loader.exec_module(wfs_index)


class WfsIndexTests(unittest.TestCase):
    def test_tiles_cover_bounds_without_gaps(self):
        tiles = wfs_index.tile_bounds((0, 0, 60, 40), 25)
        self.assertEqual(len(tiles), 6)
        self.assertEqual(tiles[0], (1, 0.0, 0.0, 25.0, 25.0))
        self.assertEqual(tiles[-1], (6, 50.0, 25.0, 60.0, 40.0))

    def test_builder_deduplicates_and_reopens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            builder = wfs_index.WfsIndexBuilder(
                temp_dir,
                "https://example.test/wfs",
                "ws:e_lv_map_cable",
                "label",
                "geografischeligging",
                "EPSG:28992",
            )
            geometry = sqlite3.Binary(b"fake-wkb")
            records = [
                ("fid:1", "1", "TEST-01", 10.0, geometry),
                ("fid:1", "1", "TEST-01", 10.0, geometry),
                ("fid:2", "2", "TEST-02", 20.0, geometry),
            ]
            inserted = builder.insert_records(records)
            self.assertEqual(inserted, 2)
            builder.commit()
            built = builder.finalize("geojson", 3, {"tile_count": 2})
            path = built.path
            self.assertFalse(built.reused)
            self.assertEqual(built.meta["feature_count"], "2")
            self.assertEqual(built.meta["label_count"], "2")
            built.close()

            reopened = wfs_index.open_existing(
                temp_dir,
                "https://example.test/wfs",
                "ws:e_lv_map_cable",
                "label",
                "geografischeligging",
                "EPSG:28992",
            )
            self.assertIsNotNone(reopened)
            self.assertTrue(reopened.reused)
            self.assertEqual(reopened.path, path)
            self.assertEqual(
                reopened.connection.execute("SELECT COUNT(*) FROM wfs_rows").fetchone()[0],
                2,
            )
            reopened.close()

    def test_metadata_change_invalidates_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            builder = wfs_index.WfsIndexBuilder(
                temp_dir,
                "https://example.test/wfs",
                "ws:e_lv_map_cable",
                "label",
                "geografischeligging",
                "EPSG:28992",
            )
            builder.insert_records(
                [("fid:1", "1", "TEST", 1.0, sqlite3.Binary(b"wkb"))]
            )
            built = builder.finalize("geojson", 1)
            built.close()

            wrong = wfs_index.open_existing(
                temp_dir,
                "https://example.test/wfs",
                "ws:e_lv_map_cable",
                "ander_labelveld",
                "geografischeligging",
                "EPSG:28992",
            )
            self.assertIsNone(wrong)


if __name__ == "__main__":
    unittest.main()
