import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_NAME = "enexiskabel_testpkg"

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package

spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME + ".csv_index", ROOT / "csv_index.py"
)
csv_index = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = csv_index
spec.loader.exec_module(csv_index)


class Feedback:
    def __init__(self, canceled=False):
        self.canceled = canceled
        self.messages = []

    def isCanceled(self):
        return self.canceled

    def pushInfo(self, message):
        self.messages.append(message)

    def setProgressText(self, message):
        self.messages.append(message)


class CsvIndexTests(unittest.TestCase):
    def _write_csv(self, path, extra=""):
        path.write_text(
            "Kabel Subgroep;Lengte [kaart] (m);Type\n"
            "Kabelgroup: TEST-01;10,5;A\n"
            "TEST-02;20;B\n"
            "TEST-01;ongeldig;C\n"
            + extra,
            encoding="utf-8-sig",
        )

    def test_build_reuse_query_and_rebuild_after_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            source = temp / "landelijk.csv"
            cache = temp / "cache"
            self._write_csv(source)

            first = csv_index.open_csv_index(
                str(source),
                str(cache),
                "Kabel Subgroep",
                "Lengte [kaart] (m)",
                Feedback(),
            )
            first_path = first.path
            self.assertFalse(first.reused)
            self.assertEqual(first.stats["total_rows"], 3)
            self.assertEqual(first.stats["valid_unique_labels"], 2)
            rows = first.rows_for_labels({"TEST-01"})
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["label"], "TEST-01")
            self.assertEqual(rows[0]["length_m"], 10.5)
            self.assertEqual(rows[1]["length_error"], "ONGELDIGE_CSV_LENGTE")
            first.close()

            second = csv_index.open_csv_index(
                str(source),
                str(cache),
                "Kabel Subgroep",
                "Lengte [kaart] (m)",
                Feedback(),
            )
            self.assertTrue(second.reused)
            self.assertEqual(second.path, first_path)
            second.close()

            self._write_csv(source, "TEST-03;30;D\n")
            third = csv_index.open_csv_index(
                str(source),
                str(cache),
                "Kabel Subgroep",
                "Lengte [kaart] (m)",
                Feedback(),
            )
            self.assertFalse(third.reused)
            self.assertEqual(third.path, first_path)
            self.assertEqual(third.stats["total_rows"], 4)
            self.assertEqual(len(third.rows_for_labels({"TEST-03"})), 1)
            third.close()

    def test_cancelled_build_does_not_leave_valid_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            source = temp / "landelijk.csv"
            cache = temp / "cache"
            self._write_csv(source)

            with self.assertRaises(csv_index.CsvIndexError):
                csv_index.open_csv_index(
                    str(source),
                    str(cache),
                    "Kabel Subgroep",
                    "Lengte [kaart] (m)",
                    Feedback(canceled=True),
                )

            candidates = list(cache.glob("enexis_csv_index_*.sqlite"))
            self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
