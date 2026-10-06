"""Values saved in the dashboard, and which value applies.

The precedence rule, for every setting the dashboard edits (lowest first):

1. the built-in default;
2. the configuration file;
3. a value saved in the dashboard;
4. an environment variable (``SHIJHON_DELIVERY__BUDGET_SECONDS``): the dashboard shows the
   setting locked, "Set by the environment", and never saves it.

Saving the value the file (or the default) gives removes the saved value, so a later change
to the file applies again. A section's saved values are checked together with the rest of
the configuration (rules across settings, such as the wait cap and the budget): if they do
not fit, the whole section keeps the configuration's values and the dashboard says why.

Add-ons are a list, not values: when the configuration file has an ``[[addons]]`` list it
is applied at startup until the list is first changed in the dashboard; from then on the
stored list (the dashboard's) is kept, until the dashboard hands it back to the file.

Secrets are stored in the database like any value (it is created 0600) and never shown.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import aiosqlite
from pydantic import BaseModel, SecretStr, ValidationError

from shijhon.catalog import plugin
from shijhon.config import (
    CORE_CATALOG_KEYS,
    CatalogSettings,
    Settings,
    catalog_settings,
    one_choices,
)
from shijhon.delivery.pacing import origin
from shijhon.store import Store

# The configuration sections the dashboard edits (of [search]: the wait for the catalog,
# on the Catalog page).
SECTIONS: Final = ("delivery", "catalog", "fill", "cleanup", "search")
APPEARANCE: Final = "appearance"  # dashboard only: not in the configuration file
ADDONS: Final = "addons"  # the add-on list's owner (see above)
# In the saved catalog section: the kind whose adapter's settings are saved there (written
# with them). They are that adapter's alone: with another kind in use they are not applied,
# shown or kept, so a token saved for one catalog never reaches another.
ADAPTER: Final = "(adapter)"
# In the saved catalog section: for each saved setting that belongs to an address (the
# adapter's ``bound``, e.g. a service's key), a mark of where that address sent it when the
# value was saved (``place``). The value is that place's alone: with the address on another
# host - changed here, in the configuration file or by the environment - it is not applied,
# shown or kept, unless it is entered again with the new address.
BOUND: Final = "(bound)"
RECORDS: Final = (ADAPTER, BOUND)  # what the section holds besides settings
# Per add-on (its ID): the settings saved through a visible field of its settings form,
# each with a mark of the value saved - only those are ever shown again.
SHOWN: Final = "addon-shown"
ACCENTS: Final = ("green", "teal", "slate", "graphite", "purple")
DEFAULT_ACCENT: Final = "green"


class _Remove:
    def __repr__(self) -> str:
        return "REMOVE"


REMOVE: Final = _Remove()  # a change that removes the saved value


@dataclass(frozen=True)
class Saved:
    value: Any  # JSON-decoded
    saved_at: float
    saved_by: str


def decoded(rows: Iterable[Any]) -> dict[str, dict[str, Saved]]:
    """Saved values by section, from rows of (section, key, value, saved_at, saved_by)."""
    saved: dict[str, dict[str, Saved]] = {}
    for section, key, value, saved_at, saved_by in rows:
        try:
            data = json.loads(value)
        except ValueError:
            continue
        saved.setdefault(section, {})[key] = Saved(data, float(saved_at), str(saved_by))
    return saved


def encode(value: Any) -> str:
    """JSON for a setting's value; secrets as their text (never shown back)."""
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    elif isinstance(value, Path):
        value = str(value)
    return json.dumps(value)


class SavedSettings:
    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    async def all(self, conn: aiosqlite.Connection | None = None) -> dict[str, dict[str, Saved]]:
        """Every saved value (``conn``: read in the caller's transaction)."""
        sql = "SELECT section, key, value, saved_at, saved_by FROM saved_settings"
        if conn is None:
            rows = await self.store.fetchall(sql)
        else:
            async with conn.execute(sql) as cursor:
                rows = list(await cursor.fetchall())
        return decoded(
            (row["section"], row["key"], row["value"], row["saved_at"], row["saved_by"])
            for row in rows
        )

    async def section(
        self, name: str, conn: aiosqlite.Connection | None = None
    ) -> dict[str, Saved]:
        return (await self.all(conn)).get(name, {})

    async def put(self, section: str, changes: Mapping[str, Any], by: str) -> None:
        """Save (or with :data:`REMOVE`, delete) values of one section together."""
        async with self.store.transaction() as conn:
            await self.write(conn, section, changes, by)

    async def write(
        self, conn: aiosqlite.Connection, section: str, changes: Mapping[str, Any], by: str
    ) -> None:
        """``put`` within the caller's transaction: saved with what else it writes, or not
        at all."""
        now = self.clock()
        for key, value in changes.items():
            if value is REMOVE:
                await conn.execute(
                    "DELETE FROM saved_settings WHERE section = ? AND key = ?", [section, key]
                )
            else:
                await conn.execute(
                    "INSERT INTO saved_settings (section, key, value, saved_at, saved_by)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (section, key) DO UPDATE SET"
                    " value = excluded.value, saved_at = excluded.saved_at,"
                    " saved_by = excluded.saved_by",
                    [section, key, encode(value), now, by],
                )

    async def accent(self) -> str:
        value = (await self.section(APPEARANCE)).get("accent")
        return value.value if value is not None and value.value in ACCENTS else DEFAULT_ACCENT

    async def addons_owner(self) -> Saved | None:
        """Set once the add-on list was changed in the dashboard (the dashboard owns it)."""
        return (await self.section(ADDONS)).get("dashboard")

    async def take_addons(self, by: str) -> None:
        if await self.addons_owner() is None:
            await self.put(ADDONS, {"dashboard": True}, by)

    async def hand_back_addons(self, by: str) -> None:
        await self.put(ADDONS, {"dashboard": REMOVE}, by)


def field_names(model: BaseModel) -> list[str]:
    return list(type(model).model_fields)


def merged(configured: BaseModel, values: Mapping[str, Any]) -> BaseModel:
    """The section with ``values`` over the configuration's, validated as a whole (rules
    across settings included). Raises ValidationError. The catalog section is the one of
    the kind that results. When that is not the configuration's kind, the configuration's
    adapter settings are another catalog's and are left out."""
    model: type[BaseModel] = type(configured)
    carried = field_names(configured)
    if isinstance(configured, CatalogSettings):
        kind = str(values.get("kind", configured.kind))
        model = catalog_settings(kind)  # an unknown kind is refused when validated
        if kind != configured.kind:
            carried = [name for name in carried if name in CORE_CATALOG_KEYS]
    names = list(model.model_fields)
    data = {name: getattr(configured, name) for name in carried if name in names}
    data.update({key: value for key, value in values.items() if key in names})
    return model.model_validate(data)


def configured_for(configured: CatalogSettings, kind: str) -> CatalogSettings | None:
    """The configuration's catalog section as it holds for the catalog ``kind`` (see
    ``merged``); None when its values do not fit that kind's settings."""
    if kind == configured.kind:
        return configured
    try:
        found = merged(configured, {"kind": kind})
    except ValidationError:
        return None
    assert isinstance(found, CatalogSettings)
    return found


def problem_text(error: ValidationError, section: str) -> str:
    """A validation error without the values (they may be secrets)."""
    parts = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"])
        message = str(item["msg"]).removeprefix("Value error, ")
        parts.append(f"{section}.{where}: {message}" if where else message)
    return "; ".join(parts)


def catalog_problem(catalog: Any) -> tuple[str, str, str] | None:
    """What would stop the catalog from being built at startup (and Shijhon with it), as
    (setting, what to enter, the page notice's rule); None when it can be built. Its
    adapter says (``plugin.Adapter.problem``; it may read files)."""
    adapter = plugin.find(catalog.kind)
    if adapter is None or adapter.problem is None:
        return None
    found = adapter.problem(catalog)
    return None if found is None else (found.setting, found.row, found.notice)


# Checks beyond the section's own validation: what building the services needs.
CHECKS: dict[str, Callable[[Any], tuple[str, str, str] | None]] = {
    "catalog": catalog_problem,
}


Locked = Mapping[str, Mapping[str, str]]  # section -> key -> the variable that sets it


def _together(keys: dict[str, str], groups: tuple[tuple[str, ...], ...]) -> dict[str, str]:
    """Settings that are one choice: the environment setting one of them locks them all (a
    token from the environment must not lose to a token file saved here)."""
    for group in groups:
        names = [keys[key] for key in sorted(group) if key in keys]
        if names:
            keys.update({key: keys.get(key, names[0]) for key in group})
    return keys


def locked_keys(settings: Settings) -> dict[str, dict[str, str]]:
    """The dashboard's settings the environment sets (``Settings.from_environment``, which
    ``load_settings`` fills), with the variable to show. The catalog's are those of the
    configuration's kind; ``catalog_locks`` gives them for another."""
    found: dict[str, dict[str, str]] = {}
    for section in SECTIONS:
        fields = set(type(getattr(settings, section)).model_fields)
        keys = {
            key: name
            for key, name in settings.from_environment.get(section, {}).items()
            if key in fields
        }
        if section == "catalog":
            keys = _together(keys, one_choices(settings.catalog.kind))
        if keys:
            found[section] = keys
    return found


def catalog_locks(locked: Locked, configured: CatalogSettings, kind: str) -> dict[str, str]:
    """The catalog settings the environment sets, for the catalog ``kind``. An adapter's
    settings in the environment are the configured kind's: they lock nothing of another
    kind."""
    env = dict(locked.get("catalog", {}))
    if kind == configured.kind:
        return env
    return {key: name for key, name in env.items() if key in CORE_CATALOG_KEYS}


def saved_kind(configured: CatalogSettings, saved: Mapping[str, Saved], locked: Locked) -> str:
    """The catalog kind the saved values choose: the saved one unless the environment
    sets the kind, else the configuration's."""
    item = saved.get("kind")
    if item is None or "kind" in locked.get("catalog", {}):
        return configured.kind
    return str(item.value)


def saved_owner(saved: Mapping[str, Saved]) -> str | None:
    """The kind the saved catalog adapter settings are for: the one recorded with them
    (:data:`ADAPTER`); None when none is recorded (they are then nobody's)."""
    marked = saved.get(ADAPTER)
    return str(marked.value) if marked is not None else None


def saved_for(saved: Mapping[str, Saved], kind: str) -> dict[str, Saved]:
    """The saved catalog values that are the catalog ``kind``'s: Shijhon's own settings,
    and the adapter's when that kind is their owner (``saved_owner``)."""
    theirs = saved_owner(saved) == kind
    return {
        key: item
        for key, item in saved.items()
        if key not in RECORDS and (key in CORE_CATALOG_KEYS or theirs)
    }


def place(address: Any) -> str:
    """A mark of where an address sends what goes with it: of its origin (so a change of
    its path keeps what was saved for it), of its text when no origin can be read from it,
    "" for no address. A mark, not the address: an address may itself be a secret."""
    text = (_text(address) or "").strip()
    if not text:
        return ""
    return hashlib.sha256(repr(origin(text) or text).encode()).hexdigest()


def displaced(settings: Any, saved: Mapping[str, Saved], keys: Any) -> list[str]:
    """Of the saved values ``keys``, those that belong to an address (the adapter's
    ``bound``) and were saved for another place than the address ``settings`` has
    (:data:`BOUND`), or with no place recorded (a saved removal is none of them)."""
    adapter = plugin.find(str(getattr(settings, "kind", plugin.NONE)))
    bound = adapter.bound if adapter is not None else {}
    record = saved.get(BOUND)
    marks = record.value if record is not None and isinstance(record.value, dict) else {}
    return [
        key
        for key, address in bound.items()
        if key in keys
        and not _removal(saved.get(key))
        and marks.get(key) != place(getattr(settings, address, None))
    ]


def _removal(item: Saved | None) -> bool:
    """A saved "no value": the dashboard's removal of the configuration's value. It is
    nothing to send anywhere, so it has no place - and stays whatever the address is (a
    removed credential never comes back with a change of address, or of the build)."""
    return item is not None and item.value is None


def in_place(
    configured: BaseModel, values: Mapping[str, Any], saved: Mapping[str, Saved], keys: Any
) -> tuple[BaseModel, list[str]]:
    """``merged``, without the saved values ``keys`` whose address is another now
    (``displaced``; the configuration's values take their place, and are held back in turn
    where they are not the address's, ``unbound``): the section, and the keys left out."""
    candidate = merged(configured, values)
    gone = displaced(candidate, saved, keys) if isinstance(configured, CatalogSettings) else []
    if gone:
        candidate = merged(configured, {k: v for k, v in values.items() if k not in gone})
    return candidate, gone


def hidden_by_environment(
    saved: Mapping[str, Mapping[str, Saved]], locked: Locked, configured: Settings | None = None
) -> list[str]:
    """Saved values not in use because the environment sets their setting."""
    found = []
    for section, keys in locked.items():
        values = saved.get(section, {})
        if section == "catalog" and configured is not None:
            kind = saved_kind(configured.catalog, values, locked)
            keys = catalog_locks(locked, configured.catalog, kind)
            values = saved_for(values, kind)
        found += [f"{section}.{key}" for key in keys if key in values]
    return found


def out_of_place(
    configured: Settings, saved: Mapping[str, Mapping[str, Saved]], locked: Locked
) -> list[tuple[str, str]]:
    """Saved catalog values not in use because the address they belong to is another now
    than the one they were saved for (``displaced``), as (setting, its address setting)."""
    items = saved.get("catalog", {})
    kind = saved_kind(configured.catalog, items, locked)
    env = catalog_locks(locked, configured.catalog, kind)
    values = {key: item.value for key, item in saved_for(items, kind).items() if key not in env}
    try:
        gone = in_place(configured.catalog, values, items, values)[1]
    except ValidationError:
        return []  # (the whole section is not in use, and said so)
    adapter = plugin.find(kind)
    return [(key, adapter.bound[key]) for key in gone] if adapter is not None else []


def effective(
    configured: Settings,
    saved: Mapping[str, Mapping[str, Saved]],
    *,
    locked: Locked,
    startup: bool = False,
) -> tuple[Settings, dict[str, str]]:
    """The settings that apply, and the sections whose saved values were not applied (with
    the reason, never a value). ``configured`` is the file over the defaults with the
    environment over both; a saved value applies unless the environment sets that key
    (``locked``). A section whose saved values do not validate together with the
    configuration keeps the configuration's values; at ``startup`` also one with which
    Shijhon could not start (a token file gone: that check reads files). A catalog
    adapter's saved settings apply only with the kind they were saved for."""
    updates: dict[str, BaseModel] = {}
    problems: dict[str, str] = {}
    for section in SECTIONS:
        env: Mapping[str, str] = locked.get(section, {})
        items: Mapping[str, Saved] = saved.get(section, {})
        if section == "catalog":
            kind = saved_kind(configured.catalog, items, locked)
            env = catalog_locks(locked, configured.catalog, kind)
            items = saved_for(items, kind)
        values = {key: item.value for key, item in items.items() if key not in env}
        if not values:
            continue
        try:  # (a value saved for another address than the one that applies is left out)
            candidate, _ = in_place(
                getattr(configured, section), values, saved.get(section, {}), values
            )
        except ValidationError as exc:
            problems[section] = problem_text(exc, section)
            continue
        if section == "catalog":  # before the check: it must see what the catalog gets
            candidate = unbound(configured.catalog, candidate)
        check = CHECKS.get(section) if startup else None
        found = check(candidate) if check is not None else None
        if found is not None and check is not None and check(getattr(configured, section)) is None:
            problems[section] = f"{section}.{found[0]}: {found[1]}"
            continue
        updates[section] = candidate
    return configured.model_copy(update=updates), problems


def _text(value: Any) -> str | None:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value) if value is not None else None


def _elsewhere(configured: CatalogSettings, applying: Any) -> list[tuple[str, str]]:
    """The configuration's settings that belong to the address another setting names (the
    adapter's ``bound``: e.g. the headers for a token service, often a key), while the
    address that applies - one saved in the dashboard - is on another host: as (setting, its
    address setting). They were set for the configuration's address alone, and are not sent
    to this one. (A value of the setting saved in the dashboard itself goes with the address
    it was saved for: ``displaced``.) The configuration's values are those it holds for the
    catalog that applies."""
    adapter = plugin.find(applying.kind)
    bound = adapter.bound if adapter is not None else {}
    base = configured_for(configured, applying.kind) if bound else None
    found = []
    for key, address in bound.items():
        theirs = getattr(base, key, None)
        target = _text(getattr(applying, address, None))
        if not theirs or target is None:
            continue
        now = getattr(applying, key, None)
        if now and now != theirs:
            continue  # the dashboard's own value
        known = _text(getattr(base, address, None))
        if origin(target) is None or origin(target) != origin(known):
            found.append((key, address))
    return found


def unbound(configured: CatalogSettings, candidate: Any) -> Any:
    """``candidate`` without the configuration's settings that must not go to its address
    (``_elsewhere``): they are back at their defaults."""
    fields = type(candidate).model_fields
    held = {
        key: fields[key].get_default(call_default_factory=True)
        for key, _ in _elsewhere(configured, candidate)
    }
    return candidate.model_copy(update=held) if held else candidate


def withheld(configured: CatalogSettings, applying: Any) -> list[tuple[str, str]]:
    """The configured settings held back from the address in use, as (setting, its address
    setting) - said at startup and on the Catalog page: a catalog whose address in use
    is one saved in the dashboard on another host than the configuration's. An address that
    is not the one used (an earlier setting of its choice is set, e.g. a token makes a token
    service unused) is nothing to say."""
    if applying.kind == plugin.NONE:
        return []
    found = []
    for key, address in _elsewhere(configured, applying):
        group = next((g for g in one_choices(applying.kind) if address in g), (address,))
        used = next((name for name in group if getattr(applying, name, None) is not None), None)
        if used == address:
            found.append((key, address))
    return found


# --- the command line (``shijhon saved``): when a saved value keeps Shijhon from starting --


def _secret(section: str, key: str, owner: str | None = None) -> bool:
    """Whether a saved value is not to be shown. A catalog adapter's setting is shown only
    when the adapter it was saved for (``owner``, ``saved_owner``) is installed and declares
    it neither a secret nor a mapping (whose values may be secrets)."""
    from shijhon.dashboard.sections import fields_of

    if section not in SECTIONS:
        return False
    hidden = ("secret", "mapping")
    if section == "catalog" and key not in CORE_CATALOG_KEYS and key != ADAPTER:
        if owner is None or plugin.find(owner) is None:
            return True
        declared = [f for f in fields_of(section, owner) if f.key == key]
        return not declared or any(f.kind in hidden for f in declared)
    return any(f.key == key and f.kind in hidden for f in fields_of(section))


def effective_from_database(configured: Settings, database: Path) -> Settings:
    """The settings that apply with the values saved in ``database`` (read only), for
    commands that run beside or instead of Shijhon (``fills-undo``'s fill policy). Trouble
    reading it raises (sqlite3.Error): a command must not fall back to other values
    silently."""
    import sqlite3
    from contextlib import closing

    if not database.exists():
        return configured
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT section, key, value, saved_at, saved_by FROM saved_settings"
        ).fetchall()
    return effective(configured, decoded(rows), locked=locked_keys(configured), startup=True)[0]


def list_saved(database: Path) -> str:
    """The saved values, one a line; secrets as "(secret)"."""
    import sqlite3

    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "SELECT section, key, value, saved_at, saved_by FROM saved_settings"
            " ORDER BY section, key"
        ).fetchall()
    if not rows:
        return "no values saved in the dashboard"
    owner: str | None = None  # the kind the saved catalog adapter settings are for
    for section, key, value, _, _ in rows:
        if (section, key) == ("catalog", ADAPTER):
            try:
                owner = str(json.loads(value))
            except ValueError:
                owner = None
    lines = []
    for section, key, value, saved_at, saved_by in rows:
        shown = "(secret)" if _secret(section, key, owner) else value
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(saved_at))
        lines.append(f"{section}.{key} = {shown}  (saved {when} by {saved_by})")
    return "\n".join(lines)


def clear_saved(database: Path, names: list[str]) -> int:
    """Remove saved values: a whole section, or ``section.key``."""
    import sqlite3

    if not database.exists():
        raise FileNotFoundError(f"no database at {database}")
    removed = 0
    with sqlite3.connect(database) as conn:
        for name in names:
            section, _, key = name.partition(".")
            if not key:
                cursor = conn.execute("DELETE FROM saved_settings WHERE section = ?", [section])
                removed += cursor.rowcount
                continue
            cursor = conn.execute(
                "DELETE FROM saved_settings WHERE section = ? AND key = ?", [section, key]
            )
            removed += cursor.rowcount
    return removed
