import pathlib
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import shape_archive


def write_shape_set(archive, folder, stem):
    for ext, data in (
        (".shp", b"shape"),
        (".dbf", b"dbf"),
        (".shx", b"shx"),
        (".prj", b"prj"),
    ):
        archive.writestr(folder + "/" + stem + ext, data)


with tempfile.TemporaryDirectory() as temp_dir:
    root = pathlib.Path(temp_dir)

    generated_cache = (
        root
        / ("processing_" + "a" * 12)
        / ("b" * 32)
        / "CACHE_FOLDER"
    )
    archive_cache, extracted_cache = shape_archive.archive_paths(str(generated_cache))
    assert shape_archive.DEFAULT_CACHE_DIRNAME in archive_cache
    assert "processing_" not in extracted_cache.casefold(), extracted_cache

    # Independently verify that direct extraction also works when a caller gives
    # a deep path of the same order as the reported QGIS failure.
    parent = root / ("processing_" + "a" * 24) / ("b" * 32) / "CACHE_FOLDER" / "enexis_shape_source"
    parent.mkdir(parents=True)
    destination = parent / shape_archive.EXTRACTED_NAME
    archive_path = root / "source.zip"
    folder = "top/" + shape_archive.TARGET_FOLDER
    with zipfile.ZipFile(archive_path, "w") as archive:
        write_shape_set(archive, folder, "imkl_elektriciteitskabel_e_lv_map_cable_noord_ligging")
        write_shape_set(archive, folder, "imkl_elektriciteitskabel_e_lv_map_cable_zuid_ligging")

    result = shape_archive.extract_target_folder(str(archive_path), str(destination))
    for region in ("noord", "zuid"):
        path = pathlib.Path(result[region])
        assert path.exists(), path
        assert len(str(path)) < shape_archive.WINDOWS_SAFE_PATH_LIMIT, (len(str(path)), path)

print("Windows deep SHAPE extraction smoke test OK")
