"""Disposable native-web visibility proof with a PREPARED synthetic collection source.

Run ``python -m tests.harness.collection_preview --rows 100000 --port 4761``.
Twenty real three-second tones support playback; extra rows are invented metadata,
not audio files. This fixture measures query preparation, not the initial sign-in
source scan. Credentials are synthetic and printed without native bearer material.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from tests.harness.collection_app import CollectionApp
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import NavidromeInstance
from tests.harness.running import RunningServer


def seed_native(nd: NavidromeInstance, rows: int) -> None:
    for i in range(4):
        write_album(
            nd.music,
            simple_album(
                f"Invented Artist {i % 2}",
                f"Collection Album {i}",
                5,
                genre="Fiction" if i % 2 else "Imaginary",
            ),
        )
    nd.scan(full=True)
    with sqlite3.connect(nd.data / "navidrome.db") as conn:
        conn.row_factory = sqlite3.Row
        template = dict(conn.execute("SELECT * FROM media_file LIMIT 1").fetchone())
        columns = ",".join('"' + column + '"' for column in template)
        values = ",".join("?" for _ in template)
        for start in range(0, rows, 500):
            batch = []
            for i in range(start, min(start + 500, rows)):
                item = {
                    **template,
                    "id": f"fixture-{i:06}",
                    "path": f"synthetic/{i:06}.flac",
                    "title": f"Generated Track {i:06}",
                    "order_title": f"generated track {i:06}",
                    "full_text": f"generated track {i:06}",
                    "search_normalized": f"generated track {i:06}",
                }
                batch.append(tuple(item.values()))
            conn.executemany(f"INSERT INTO media_file({columns}) VALUES ({values})", batch)  # noqa: S608
        for album in conn.execute(
            "SELECT album_id,COUNT(*) songs,SUM(duration) duration "
            "FROM media_file GROUP BY album_id"
        ).fetchall():
            conn.execute(
                "UPDATE album SET song_count=?,duration=? WHERE id=?",
                (album["songs"], album["duration"], album["album_id"]),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=12000)
    parser.add_argument("--port", type=int, default=4761)
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()
    if not 0 <= args.rows <= 100000:
        parser.error("choose 0 through 100000 extra synthetic rows")
    root = Path(tempfile.mkdtemp(prefix="shijhon-visibility-preview-"))
    nd = NavidromeInstance(root / "native", binary=args.binary)
    server = None
    try:
        nd.start()
        nd.create_admin()
        seed_native(nd, args.rows)
        users = {
            name: nd.create_user(name, "fixture-password")
            for name in ("fixture-listener", "fixture-second")
        }
        songs = []
        for start in range(0, args.rows + 20, 5000):
            songs.extend(
                nd.native(
                    "GET",
                    "song",
                    params={
                        "_start": start,
                        "_end": start + 5000,
                        "_sort": "id",
                        "missing": "false",
                    },
                ).json()
            )
        server = RunningServer(CollectionApp(nd, root / "proxy", songs), port=args.port)
        server.start()
        details = {
            "root": str(root),
            "url": server.base_url,
            "native": nd.base_url,
            "users": users,
            "tracks": len(songs),
            "password": "fixture-password",
        }
        (root / "details.json").write_text(json.dumps(details))
        print(json.dumps({**details, "ready": True}), flush=True)
        peak = 0
        while True:
            sizes = {}
            for path in (root / "proxy/private").glob("queries.sqlite3*"):
                # WAL may disappear after a checkpoint.
                with suppress(FileNotFoundError):
                    sizes[path.name] = path.stat().st_size
            peak = max(peak, sum(sizes.values()))
            (root / "disk-peak.json").write_text(json.dumps({"peak": peak, "files": sizes}))
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.stop()
        nd.stop()


if __name__ == "__main__":
    main()
