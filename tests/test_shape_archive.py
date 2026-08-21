import importlib.util
import io
import ntpath
import pathlib
import sys
import tempfile
import types
import unittest
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_NAME = "enexiskabel_shape_testpkg"

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package

spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME + ".shape_archive", ROOT / "shape_archive.py"
)
shape_archive = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shape_archive
spec.loader.exec_module(shape_archive)


class ShapeArchiveTests(unittest.TestCase):
    def _write_shape_set(self, archive, folder, stem):
        for ext, data in (
            (".shp", b"shape"),
            (".dbf", b"dbf"),
            (".shx", b"shx"),
            (".prj", b"prj"),
            (".cpg", b"UTF-8"),
        ):
            archive.writestr(folder + "/" + stem + ext, data)

    def _valid_zip_bytes(self):
        buffer = io.BytesIO()
        folder = "top/" + shape_archive.TARGET_FOLDER
        with zipfile.ZipFile(buffer, "w") as archive:
            self._write_shape_set(archive, folder, "kabels_noord")
            self._write_shape_set(archive, folder, "kabels_zuid")
        return buffer.getvalue()

    def test_uses_verified_direct_enexis_shape_archive(self):
        self.assertEqual(
            shape_archive.SHAPE_DOWNLOAD_URL,
            "https://enxp433-opendata-publications.s3.eu-west-1.amazonaws.com/"
            "Open_Asset_Data_Elektra.zip",
        )
        self.assertNotIn("spotler", shape_archive.SHAPE_DOWNLOAD_URL.casefold())
        self.assertNotIn("_CSV.zip", shape_archive.SHAPE_DOWNLOAD_URL)
        self.assertIn(shape_archive.DOWNLOAD_ID, shape_archive.ARCHIVE_NAME)
        self.assertIn(shape_archive.DOWNLOAD_ID, shape_archive.EXTRACTED_NAME)

    def test_windows_qgis_temp_paths_stay_below_safe_limit(self):
        # Synthetic path with the same shape as a deep QGIS Processing temp path.
        cache_root = (
            r"C:\Users\TestUserLongCompanyProfile\AppData\Local\Temp\processing_abcdefgh"
            r"\0123456789abcdef0123456789abcdef\CACHE_FOLDER\enexis_shape_source"
        )
        filename = "imkl_elektriciteitskabel_e_lv_map_cable_zuid_ligging.dbf"

        old_path = ntpath.join(
            cache_root,
            "enexis_shape_extract_12345678",
            shape_archive.TARGET_FOLDER,
            filename,
        )
        new_stage_path = ntpath.join(
            cache_root,
            shape_archive.EXTRACT_STAGE_PREFIX + "12345678",
            filename,
        )
        new_cached_path = ntpath.join(
            cache_root,
            shape_archive.EXTRACTED_NAME,
            filename,
        )

        self.assertGreaterEqual(len(old_path), 260)
        self.assertLess(len(new_stage_path), shape_archive.WINDOWS_SAFE_PATH_LIMIT)
        self.assertLess(len(new_cached_path), shape_archive.WINDOWS_SAFE_PATH_LIMIT)

    def test_streamed_download_writes_valid_zip_atomically(self):
        data = self._valid_zip_bytes()

        class Response:
            def __init__(self, payload):
                self.buffer = io.BytesIO(payload)
                self.headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                return self.buffer.read(size)

        original = shape_archive.urlopen
        shape_archive.urlopen = lambda request, timeout=None: Response(data)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = pathlib.Path(temp_dir) / "download.zip"
                shape_archive.download_archive(str(destination))
                self.assertTrue(destination.exists())
                self.assertTrue(zipfile.is_zipfile(destination))
                self.assertEqual(destination.read_bytes(), data)
        finally:
            shape_archive.urlopen = original

    def test_extracts_only_target_folder_and_finds_noord_zuid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            archive_path = temp / "source.zip"
            target = temp / "extracted"
            folder = "top/" + shape_archive.TARGET_FOLDER
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("top/other/ignore.txt", "ignore")
                self._write_shape_set(archive, folder, "kabels_noord")
                self._write_shape_set(archive, folder, "kabels_zuid")

            found = shape_archive.extract_target_folder(
                str(archive_path), str(target)
            )
            self.assertEqual(set(found), {"noord", "zuid"})
            self.assertTrue(pathlib.Path(found["noord"]).exists())
            self.assertTrue(pathlib.Path(found["zuid"]).exists())
            self.assertFalse((target / "ignore.txt").exists())

    def test_missing_region_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            folder = temp / "extracted"
            folder.mkdir()
            for ext in (".shp", ".dbf", ".shx", ".prj"):
                (folder / ("kabels_noord" + ext)).write_bytes(b"x")
            with self.assertRaises(shape_archive.ShapeArchiveError):
                shape_archive.discover_shape_files(str(folder))

    def test_missing_sidecar_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            folder = temp / "extracted"
            folder.mkdir()
            for stem in ("kabels_noord", "kabels_zuid"):
                for ext in (".shp", ".dbf", ".shx"):
                    (folder / (stem + ext)).write_bytes(b"x")
            with self.assertRaises(shape_archive.ShapeArchiveError):
                shape_archive.discover_shape_files(str(folder))

    def test_path_traversal_member_is_not_extracted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            archive_path = temp / "source.zip"
            target = temp / "extracted"
            folder = "top/" + shape_archive.TARGET_FOLDER
            with zipfile.ZipFile(archive_path, "w") as archive:
                self._write_shape_set(archive, folder, "kabels_noord")
                self._write_shape_set(archive, folder, "kabels_zuid")
                archive.writestr(folder + "/../escape.txt", "no")
            shape_archive.extract_target_folder(str(archive_path), str(target))
            self.assertFalse((temp / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
