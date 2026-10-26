from pathlib import Path

import pytest

from shijhon.config import load_settings
from shijhon.music_imports import ImportLimits


def test_imports_are_opt_in_and_defaults_match_service_limits():
    settings = load_settings(None)
    assert not settings.music_imports.enabled
    assert settings.music_imports.folder == "_imports"
    assert (
        ImportLimits(**settings.music_imports.model_dump(exclude={"enabled", "folder"}))
        == ImportLimits()
    )


@pytest.mark.parametrize(
    "folder", ["", ".", "..", "../x", "/imports", "a/b", "a\\b", ".hidden", "a:b"]
)
def test_import_folder_must_be_a_dedicated_child(folder):
    with pytest.raises(ValueError):
        load_settings(None, music_imports={"folder": folder})


@pytest.mark.parametrize("managed", ["_imports", "./_imports/", "_imports/catalog"])
def test_imports_cannot_share_disposable_catalog_storage(managed):
    with pytest.raises(ValueError, match="separate folders"):
        load_settings(None, placeholders={"folder": managed})


def test_enabled_imports_need_library_path_and_environment_can_enable(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="library_path"):
        load_settings(None, music_imports={"enabled": True})
    monkeypatch.setenv("SHIJHON_MUSIC_IMPORTS__ENABLED", "true")
    monkeypatch.setenv("SHIJHON_MUSIC_IMPORTS__FOLDER", "Personal-Audio")
    settings = load_settings(None, navidrome={"library_path": tmp_path})
    assert settings.music_imports.enabled
    assert settings.music_imports.folder == "Personal-Audio"
    assert settings.navidrome.library_path == Path(tmp_path)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_file_bytes": 513 * 1024**3},
        {"max_job_bytes": 513 * 1024**3},
        {"max_owner_bytes": 2 * 1024**4},
        {"max_uploads": 33},
        {"max_job_files": 1_000_001},
        {"min_free_bytes": -1},
    ],
)
def test_import_limits_are_bounded_and_consistent(limits):
    with pytest.raises(ValueError):
        load_settings(None, music_imports=limits)
