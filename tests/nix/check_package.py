"""Check the installed native environment against the application's existing lock."""

import importlib.metadata
import importlib.resources
import sys
import tomllib

from shijhon.catalog import plugin

with open(sys.argv[1], "rb") as source:
    locked = tomllib.load(source)
versions = {(p["name"], p["version"]) for p in locked["package"]}
installed = {
    (p.metadata["Name"].lower().replace("_", "-"), p.version)
    for p in importlib.metadata.distributions()
}
assert installed <= versions, installed - versions
assert not {"pytest", "ruff", "mypy", "shijhon-demo-catalog"} & {p[0] for p in installed}
assert sys.version_info >= (3, 12)
assert set(plugin.installed()) == {"addon"}
assert not plugin.broken()
root = importlib.resources.files("shijhon")
assert root.joinpath("dashboard/templates/base.html").is_file()
assert root.joinpath("dashboard/static/fonts/OFL.txt").is_file()
assert any(root.joinpath("store/migrations").iterdir())
print(f"{len(installed)} locked runtime distributions; CLI, assets and adapter discovery pass")
