from __future__ import annotations

import ctypes
import dataclasses
import errno
import os
import sys
from pathlib import Path

import pytest

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.delivery.download_first import DownloadFirst
from shijhon.placeholders import durable
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import _beside, _other_suffixes
from shijhon.placeholders.layout import Layout, LayoutError, safe_name
from shijhon.placeholders.silence import samples_for, silent_flac
from shijhon.placeholders.tags import ALBUM_TAGS, mapped


def test_mapped_merges_aliases_in_navidrome_order() -> None:
    raw = {"year": ["2013"], "releasedate": ["2012-03-01"], "tdrl": ["2012-03-01"]}
    assert mapped(raw, ALBUM_TAGS["releasedate"]) == ["2012-03-01", "2013"]


def test_mapped_skips_blank_values() -> None:
    assert mapped({"album": ["  ", "X"]}, ALBUM_TAGS["album"]) == ["X"]


def test_safe_names() -> None:
    assert safe_name("NO/SE: Live?") == "NO_SE_ Live_"
    assert safe_name("...") == "_"
    assert len(safe_name("é" * 200, 80).encode()) <= 80


# Names a catalog could give an artist, an album or a song.
HOSTILE = [
    ". .." + " " * 100 + "x",  # the cut at the limit used to leave ".."
    "..",
    ".",
    " . . ",
    ". .staging",
    ". ..",
    "../../outside",
    "..\\..\\outside",
    "\x00..\x00",
    "/",
    "x" + "." * 200,
    "." * 79 + "x",
    ". " * 60 + "x",
    "ab" + " " * 77 + ".." + "x" * 10,  # the cut leaves "ab" + spaces + "."
    "\u00a0..\u00a0",
    "\u3000. .\u3000",
    "é" * 39 + "x.." + "y" * 10,  # the cut falls after the dots
    "",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_no_name_leaves_its_folder_or_hides(text: str) -> None:
    for limit in (80, 100):
        name = safe_name(text, limit)
        assert name and name not in (".", "..")
        assert not set(name) & set("/\\\x00")
        assert name == name.strip().strip(".") and not name.startswith(".")
        assert len(name.encode()) <= limit
    layout = Layout(Path("/music"), "_shijhon")
    folder = layout.release_folder(CatalogRef("test", "1"), text, text)
    assert Path(folder).parts[0] == "_shijhon" and len(Path(folder).parts) == 3
    resolved = Path(os.path.normpath(Path("/music") / folder))
    assert resolved.parent.parent == Path("/music/_shijhon")
    assert resolved.parent.name != ".staging"
    assert layout.contained(f"{folder}/1-01 x.flac") == Path("/music") / folder / "1-01 x.flac"


def test_ordinary_names_are_kept() -> None:
    for name in ("Mr. Lantern", "A.B.C.x", "A  B", "été", "St. Harbor [Deluxe]", "a" * 80):
        assert safe_name(name) == name
    assert safe_name("...And More") == "And More"
    assert safe_name("Wait...") == "Wait"


@pytest.mark.parametrize("folder", ["_shijhon", "music/_placeholders"])
def test_only_paths_inside_the_placeholder_folder_are_written(folder: str) -> None:
    layout = Layout(Path("/library"), folder)
    inside = f"{folder}/Artist/Album [0a1b2c3d]"
    assert layout.contained(inside) == Path("/library") / inside
    song = f"{inside}/1-01 Song.flac"
    assert layout.contained(song) == Path("/library") / song
    for outside in (
        "",
        folder,
        f"{folder}/",
        f"{folder}/..",
        f"{folder}/../x.flac",
        f"{folder}/Artist/../../x.flac",
        f"{folder}/Artist/../x.flac",  # inside, but never a name the layout makes
        f"{folder}/./x.flac",
        f"{folder}/.staging",
        f"{folder}/.staging/x.flac",
        f"{folder}X/x.flac",
        "../x.flac",
        "..",
        ".",
        "Artist/Album/x.flac",
        "/library/" + inside,
        "/etc/x.flac",
        f"{inside}/x\x00.flac",
    ):
        with pytest.raises(LayoutError):
            layout.contained(outside)


def test_a_staging_folder_that_is_a_link_out_is_refused(tmp_path: Path) -> None:
    """Files are prepared, backed up and cleaned up in the staging folder: one that is a
    link out of the placeholder folder is not used (the placeholder folder itself may be
    a link), and its ignore file is never written through a link."""
    elsewhere = tmp_path / "owned"
    elsewhere.mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "_shijhon").symlink_to(tmp_path / "real", target_is_directory=True)
    layout = Layout(tmp_path / "library", "_shijhon")
    layout.ensure()  # the placeholder folder a link: fine
    assert (tmp_path / "real" / ".staging" / ".ndignore").read_text() == "*\n"
    layout.contained("_shijhon/Artist/Album [0a1b2c3d]/1-01 x.flac", resolve=True)
    (tmp_path / "real" / ".staging" / ".ndignore").unlink()
    (tmp_path / "real" / ".staging" / ".ndignore").symlink_to(elsewhere / "made")
    layout.ensure()
    assert not (elsewhere / "made").exists()  # a link left there: not written through
    (tmp_path / "real" / ".staging" / ".ndignore").unlink()
    (tmp_path / "real" / ".staging").rmdir()
    (tmp_path / "real" / ".staging").symlink_to(elsewhere, target_is_directory=True)
    for use in (layout.ensure, lambda: layout.staging, lambda: layout.contained("_shijhon/a/b")):
        with pytest.raises(LayoutError, match="staging folder is a link"):
            use()
    assert list(elsewhere.iterdir()) == []


def test_release_folder_is_neutral_and_stable() -> None:
    layout = Layout(Path("/music"), "_shijhon")
    ref = CatalogRef("demo", "900000001")
    folder = layout.release_folder(ref, "Artist", "Album")
    assert folder.startswith("_shijhon/Artist/Album [")
    assert "demo" not in folder and "900000001" not in folder
    assert folder == layout.release_folder(ref, "Artist", "Album")
    assert layout.is_placeholder_path(folder + "/x.flac")
    assert not layout.is_placeholder_path("_shijhonX/x.flac")


def test_file_name() -> None:
    track = CatalogTrack(CatalogRef("t", "1"), "Intro / Outro", "A", 1000, disc=2, number=3)
    assert Layout.file_name(track) == "2-03 Intro _ Outro.flac"


def test_a_file_written_to_replace_another_never_has_its_size(tmp_path: Path) -> None:
    """A retag with tags of the same length would give a file of the same size: Navidrome's
    record of the size could then not tell whether it read the new file."""

    def silent(name: str, comments: dict[str, list[str]], **kwargs: int | None) -> Path:
        path = tmp_path / name
        path.write_bytes(silent_flac(samples_for(3000)))
        tagging.write_flac(path, comments, **kwargs)
        return path

    old = silent("old.flac", {"title": ["One"], "tracknumber": ["1"]})
    new = {"title": ["Two"], "tracknumber": ["2"]}
    assert silent("same.flac", new).stat().st_size == old.stat().st_size
    other = silent("other.flac", new, not_size=old.stat().st_size)
    assert other.stat().st_size != old.stat().st_size
    assert tagging.read_vorbis(other) == new
    assert tagging.flac_samples(other) == samples_for(3000)
    untouched = silent("untouched.flac", new, not_size=1)
    assert untouched.stat().st_size == old.stat().st_size


def test_directories_made_and_synced(tmp_path: Path) -> None:
    target = tmp_path / "Artist" / "Album"
    assert durable.made_dirs(target) == [tmp_path / "Artist", target]
    target.mkdir(parents=True)
    assert durable.made_dirs(target) == []
    file = target / "a.flac"
    file.write_bytes(b"x")
    durable.sync_file(file)
    durable.sync_dirs(target, target.parent, target)  # the same one twice: once
    durable.sync_dir(tmp_path / "gone")  # a folder that is gone: nothing


def test_only_the_same_song_in_another_format_is_beside_it(tmp_path: Path) -> None:
    """The files beside a placeholder's: the same name in another audio format - never
    another song whose name starts the same; one without Shijhon's marker of the track
    (someone's own file) is never taken for the song, so never removed as a stray."""
    for name in ("1-01 Mr.flac", "1-01 Mr.m4a", "1-01 Mr. Lantern.flac", "1-01 Mr.jpg"):
        (tmp_path / name).write_bytes(b"x")
    row = {"track_ref": "demo:1"}
    assert _beside(tmp_path / "1-01 Mr.flac", row) == ([], [tmp_path / "1-01 Mr.m4a"])
    assert _other_suffixes(tmp_path / "1-01 Mr.flac", row) == []


def test_downloads_a_stop_left_behind_are_removed(tmp_path: Path) -> None:
    """Only files named as downloads go: a partial, and one whose format was known."""
    work = tmp_path / "downloads"
    work.mkdir()
    partial = work / f".{'a' * 32}.part"
    whole = work / f"{'b' * 32}.m4a"
    other = work / "notes.txt"
    for path in (partial, whole, other):
        path.write_bytes(b"x")
    (work / "folder").mkdir()
    downloads = DownloadFirst(None, None, None, work)  # type: ignore[arg-type]
    assert downloads.clear_left_over() == 2
    assert sorted(p.name for p in work.iterdir()) == ["folder", "notes.txt"]
    assert DownloadFirst(None, None, None, tmp_path / "none").clear_left_over() == 0  # type: ignore[arg-type]


def test_the_first_write_syncs_what_it_makes_up_to_the_library_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a fresh installation the first write makes the placeholder folder and its
    staging folder. Their own entries - the placeholder root's in its parent, the library
    root - and the ignore file are on disk before anything depends on them; once they are
    there, nothing more is synced."""
    library = tmp_path / "library"
    library.mkdir()
    synced: list[tuple[str, Path]] = []
    real_dir, real_file = durable.sync_dir, durable.sync_file

    def sync_dir(path: Path) -> None:
        synced.append(("dir", path))
        real_dir(path)

    def sync_file(path: Path) -> None:
        synced.append(("file", path))
        real_file(path)

    monkeypatch.setattr(durable, "sync_dir", sync_dir)
    monkeypatch.setattr(durable, "sync_file", sync_file)
    layout = Layout(library, "placeholders/_shijhon")
    layout.ensure()
    root, staging = library / "placeholders" / "_shijhon", layout.staging
    assert (staging / ".ndignore").read_text() == "*\n"
    assert synced[0] == ("file", staging / ".ndignore")  # its content, before its entry
    assert {path for kind, path in synced if kind == "dir"} == {
        staging,  # the ignore file's entry
        root,  # the staging folder's
        root.parent,  # the placeholder root's
        library,  # ... and its parent's, in the library root
    }
    synced.clear()
    layout.ensure()  # every later write: nothing made, nothing synced
    assert synced == []
    (staging / ".ndignore").unlink()
    layout.ensure()  # the ignore file alone, made again
    assert synced == [("file", staging / ".ndignore"), ("dir", staging)]
    (staging / ".ndignore").write_text("")  # left empty by a crash: written again
    layout.ensure()
    assert (staging / ".ndignore").read_text() == "*\n"


def test_a_partial_catalog_album_says_how_many_tracks_the_album_has() -> None:
    """A release whose track list could not be read in full is written with the tracks
    that were read - and with the catalog's own count as the album's total, not the
    number that happened to be readable."""
    from tests.harness.engine import catalog_release

    whole = catalog_release("total", "Total Album", "Total Artist", 3)
    assert tagging.catalog_album_tags(whole)["tracktotal"] == ["3"]
    partial = dataclasses.replace(whole, tracks=whole.tracks[:2], incomplete=True, track_count=3)
    assert tagging.catalog_album_tags(partial)["tracktotal"] == ["3"]
    counted = dataclasses.replace(whole, track_count=9)  # (whole: its tracks are the total)
    assert tagging.catalog_album_tags(counted)["tracktotal"] == ["3"]


@pytest.mark.parametrize(
    ("track_clean", "release_clean", "explicit", "expected"),
    [
        (True, False, False, ["2"]),
        (False, True, False, ["2"]),
        (True, True, True, ["1"]),
        (False, False, False, None),
    ],
)
def test_clean_advisory_keeps_track_and_release_flags(
    track_clean: bool,
    release_clean: bool,
    explicit: bool,
    expected: list[str] | None,
) -> None:
    from tests.harness.engine import catalog_release

    release = catalog_release(
        "clean-song", "Mixed Lanterns", "Paper Quartet", 1, clean=release_clean
    )
    track = dataclasses.replace(release.tracks[0], clean=track_clean, explicit=explicit)
    assert tagging.placeholder_tags({}, track, release).get("itunesadvisory") == expected


@pytest.mark.parametrize("occupied", [False, True])
def test_exclusive_publication_on_a_filesystem_without_hard_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occupied: bool,
) -> None:
    import errno

    from shijhon.placeholders.engine import _place_unused

    staged, target = tmp_path / "staged.flac", tmp_path / "final.flac"
    staged.write_bytes(b"staged audio")
    if occupied:
        target.write_bytes(b"foreign audio")

    def no_links(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOTSUP, "hard links unsupported")

    monkeypatch.setattr(os, "link", no_links)
    if occupied:
        with pytest.raises(FileExistsError):
            _place_unused(staged, target)
        assert staged.read_bytes() == b"staged audio"
        assert target.read_bytes() == b"foreign audio"
    else:
        _place_unused(staged, target)
        assert target.read_bytes() == b"staged audio"
        assert not staged.exists()


@pytest.mark.parametrize(
    "link_error",
    [errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL],
)
@pytest.mark.parametrize("native", ["missing", "unsupported", "exclusive"])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("occupied", ["free", "file", "link"])
def test_publication_falls_back_when_exclusive_operations_are_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    link_error: int,
    native: str,
    platform: str,
    occupied: str,
) -> None:
    from types import SimpleNamespace

    from shijhon.placeholders.engine import _place_unused

    staged, target = tmp_path / "staged", tmp_path / "target"
    staged.write_bytes(b"new audio")
    if occupied == "file":
        target.write_bytes(b"foreign audio")
    elif occupied == "link":
        target.symlink_to(tmp_path / "missing")

    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(link_error, "hard links unavailable")

    def rename(*args: object) -> int:
        if native == "unsupported":
            # macOS EOPNOTSUPP is distinct from ENOTSUP; Linux filesystems commonly
            # reject RENAME_NOREPLACE with EINVAL.
            ctypes.set_errno(errno.EOPNOTSUPP if platform == "darwin" else errno.EINVAL)
            return -1
        if os.path.lexists(target):
            ctypes.set_errno(errno.EEXIST)
            return -1
        os.rename(staged, target)
        return 0

    symbol = "renamex_np" if platform == "darwin" else "renameat2"
    libc = SimpleNamespace(**({} if native == "missing" else {symbol: rename}))
    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: libc)
    if occupied != "free":
        with pytest.raises(FileExistsError):
            _place_unused(staged, target)
        assert staged.read_bytes() == b"new audio"
        if occupied == "file":
            assert target.read_bytes() == b"foreign audio"
        else:
            assert target.is_symlink()
    else:
        _place_unused(staged, target)
        assert target.read_bytes() == b"new audio"
        assert not staged.exists()
    assert ("exclusive rename was unavailable" in caplog.text) == (native != "exclusive")


@pytest.mark.parametrize("operation", ["link", "rename"])
def test_publication_does_not_hide_real_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from types import SimpleNamespace

    from shijhon.placeholders.engine import _place_unused

    staged, target = tmp_path / "staged", tmp_path / "target"
    staged.write_bytes(b"new audio")

    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EACCES if operation == "link" else errno.ENOTSUP, "denied")

    def denied(*args: object) -> int:
        ctypes.set_errno(errno.EACCES)
        return -1

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(renameat2=denied))
    with pytest.raises(OSError) as failure:
        _place_unused(staged, target)
    assert failure.value.errno == errno.EACCES
    assert staged.exists() and not target.exists()


def test_delivery_retains_grouping_without_requiring_unrepresentable_metadata() -> None:
    from shijhon.placeholders.engine import _retained_metadata

    original = {
        "albumId": "album",
        "discNumber": 1,
        "trackNumber": 2,
        "releaseDate": {"year": 2004},
        "year": 2001,
        "originalDate": {"year": 1998},
        "genres": [{"name": "Invented"}],
    }
    changed = original | {"year": 2004, "originalDate": None, "genres": []}
    assert _retained_metadata(original) == _retained_metadata(changed)
    for key in ("albumId", "discNumber", "trackNumber", "releaseDate"):
        assert _retained_metadata(original) != _retained_metadata(changed | {key: None})
