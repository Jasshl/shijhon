"""A settings page generated from one configuration section: its rows, and saving it.

Saving checks every field, then the section as a whole (the rules across settings in
``config.py``), and writes nothing unless everything is valid. What is written follows the
precedence rule (``saved.py``): a value equal to the configuration's removes the saved one.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
from pydantic import BaseModel, SecretStr, ValidationError

from shijhon.catalog import plugin
from shijhon.config import CORE_CATALOG_KEYS, Settings, catalog_settings
from shijhon.dashboard.fields import (
    Group,
    Invalid,
    Meta,
    SettingField,
    format_number,
    grouped,
    section_fields,
)
from shijhon.dashboard.live import is_live
from shijhon.dashboard.saved import (
    ADAPTER,
    BOUND,
    RECORDS,
    REMOVE,
    Locked,
    Saved,
    catalog_locks,
    catalog_problem,
    configured_for,
    displaced,
    effective,
    encode,
    in_place,
    merged,
    place,
    saved_for,
    unbound,
)

CLEAR = "-clear"  # suffix of a secret's "remove" checkbox
# The Catalog page's hidden field: the kind whose adapter settings the form holds. Typed
# values of an adapter's settings are taken only for that kind (a form made for one
# catalog never fills another's settings).
SHOWN_KIND = "shown-kind"


@dataclass
class Problem:
    row: str  # in the row: what to enter
    notice: str  # in the page notice, after the setting's name


@dataclass
class Submitted:
    values: dict[str, Any]  # parsed, by key
    problems: dict[str, Problem] = field(default_factory=dict)
    candidate: BaseModel | None = None  # the section as it would apply
    removals: list[str] = field(default_factory=list)  # saved values under a lock, removed
    # The form chose another catalog than the one whose settings it held, and what is
    # missing are settings of that catalog, which the form did not have: no mistake of
    # the form's, but the first of two steps - the page with those settings comes next.
    first_step: bool = False


@dataclass
class Written:
    changed: list[str]  # keys whose applying value changed
    writes: dict[str, Any]  # saved values to write (or REMOVE)
    removed: list[str] = field(default_factory=list)  # saved values under a lock removed
    # Saved values removed because their address is on another host now, as (setting, its
    # address setting): they were not entered again with it (``saved.displaced``).
    displaced: list[tuple[str, str]] = field(default_factory=list)


@functools.cache
def _fields(section: str) -> list[SettingField]:
    model = Settings.model_fields[section].annotation
    assert isinstance(model, type) and issubclass(model, BaseModel)
    return section_fields(section, model)


def fields_of(section: str, kind: str = plugin.NONE) -> list[SettingField]:
    """The section's settings. The catalog's are Shijhon's own with those the adapter
    ``kind`` declares, in its words, after the choice of the catalog - whose choices are
    the installed adapters."""
    if section != "catalog":
        return _fields(section)
    adapter = plugin.find(kind)
    words = {
        key: Meta(
            label=w.label,
            help=w.help,
            group=w.group,
            unit=w.unit,
            spoken=w.spoken,
            options=dict(w.options),
            short=w.short,
            max_length=w.max_length,
            pattern=w.pattern,
            pattern_error=w.pattern_error,
            choices=w.choices,
        )
        for key, w in (adapter.words.items() if adapter is not None else ())
    }
    fields = section_fields(section, catalog_settings(kind), extra=words, after="kind")
    kinds = {plugin.NONE: "None", **{name: a.label for name, a in plugin.installed().items()}}

    def choice(f: SettingField) -> SettingField:
        meta = dataclasses.replace(f.meta, options=kinds)
        return dataclasses.replace(f, kind="select", values=tuple(kinds), meta=meta)

    return [choice(f) if f.key == "kind" else f for f in fields]


def kind_of(section: str, settings: Any) -> str:
    """The catalog kind of a catalog section's settings (its fields follow it)."""
    return str(settings.kind) if section == "catalog" else plugin.NONE


def form_kind(
    section: str, applying: Any, form: Mapping[str, str] | None, env: Mapping[str, str]
) -> str:
    """The catalog kind a form is about: the one it chooses (so a change of the catalog
    is checked, and shown again when refused, with the new catalog's settings), else the
    one that applies."""
    kind = kind_of(section, applying)
    if section != "catalog" or form is None or "kind" in env:
        return kind
    chosen = form.get("kind", kind)
    return chosen if chosen == plugin.NONE or plugin.find(chosen) is not None else kind


def _own(section: str, key: str) -> bool:
    """One of Shijhon's own settings (not a catalog adapter's)."""
    return section != "catalog" or key in CORE_CATALOG_KEYS


def _typed(section: str, key: str, form: Mapping[str, str], kind: str, applying: Any) -> bool:
    """Whether the form's value for ``key`` is taken: an adapter's setting only from a form
    that holds the settings of that kind (:data:`SHOWN_KIND`; a form without it: of the kind
    that applies)."""
    return _own(section, key) or form.get(SHOWN_KIND, kind_of(section, applying)) == kind


@dataclass
class _View:
    """A section's settings as a page of ``kind`` reads them."""

    env: Mapping[str, str]  # the settings the environment sets
    saved: Mapping[str, Saved]  # the saved values that are this kind's
    now: Any  # the settings that apply (None: they do not fit this kind)
    base: Any  # the configuration's (None likewise)

    kinds: tuple[str, str] = (plugin.NONE, plugin.NONE)  # the kind that applies, and the
    # configuration's (``now`` and ``base`` are both of the page's kind)

    def value(self, f: SettingField) -> Any:
        if f.section == "catalog" and f.key == "kind":
            return self.kinds[0]
        return getattr(self.now, f.key, f.builtin) if self.now is not None else f.builtin

    def default(self, f: SettingField) -> Any:
        if f.section == "catalog" and f.key == "kind":
            return self.kinds[1]
        return getattr(self.base, f.key, f.builtin) if self.base is not None else f.builtin


def _view(
    section: str,
    configured: Settings,
    saved: Mapping[str, Mapping[str, Saved]],
    locked: Locked,
    applying: Any,
    kind: str,
) -> _View:
    base = getattr(configured, section)
    items = saved.get(section, {})
    if section != "catalog":
        return _View(locked.get(section, {}), items, applying, base)
    env = catalog_locks(locked, base, kind)
    kinds = (str(applying.kind), str(base.kind))
    theirs = configured_for(base, kind)
    if kind == applying.kind:
        return _View(env, _placed(items, kind, applying), applying, theirs, kinds)
    # Another catalog than the one that applies (a form chooses it): Shijhon's own
    # settings as they apply, its adapter's as the configuration has them for that kind.
    own = {key: getattr(applying, key) for key in CORE_CATALOG_KEYS if key != "kind"}
    try:
        now: Any = merged(base, {**own, "kind": kind})
    except ValidationError:
        now = None
    return _View(env, _placed(items, kind, now), now, theirs, kinds)


def _placed(items: Mapping[str, Saved], kind: str, settings: Any) -> dict[str, Saved]:
    """The saved values that are this kind's (``saved_for``), without those saved for
    another address than the one ``settings`` has (``displaced``): they are not applied, so
    the page does not show them as saved, and the next save removes them."""
    ours = saved_for(items, kind)
    gone = displaced(settings, items, ours) if settings is not None else []
    return {key: item for key, item in ours.items() if key not in gone}


def rows(
    section: str,
    configured: Settings,
    saved: Mapping[str, Mapping[str, Saved]],
    *,
    addon_names: list[str],
    form: Mapping[str, str] | None = None,
    problems: Mapping[str, Problem] | None = None,
    just_saved: list[str] | None = None,
    date: Any = str,
    locked: Locked,
    unmarked: bool = False,
) -> list[tuple[Group, list[dict[str, Any]]]]:
    """The page's groups of rows (template context: the ``setting`` macro's row). A setting
    the environment sets is shown as text (kind "locked"): the environment's value (a
    secret's only as set or not), its variable, and a saved value it hides. ``unmarked``:
    the rows as ``problems`` make them, without saying the problems (a first step)."""
    applying = getattr(effective(configured, saved, locked=locked)[0], section)
    kind = form_kind(section, applying, form, locked.get(section, {}))
    view = _view(section, configured, saved, locked, applying, kind)
    env, saved_section = view.env, view.saved

    def sent(key: str) -> bool:
        """A failed save shows what was typed; fields its form did not have keep their
        values (a page may have several forms; a switch always sends a hidden "false")."""
        return form is not None and key in form and _typed(section, key, form, kind, applying)

    problems = problems or {}
    result = []
    for group, members in grouped(section, fields_of(section, kind)):
        group_rows = []
        for f in members:
            value, default = view.value(f), view.default(f)
            problem = problems.get(f.key)
            row: dict[str, Any] = {
                "id": f.id,
                "name": f.key,
                "kind": "select" if f.meta.choices == "sources" else f.kind,
                "label": f.meta.label,
                "help": f.meta.help,
                "unit": f.unit,
                "spoken": f.spoken,
                "short": f.meta.short,
                "max_length": f.meta.max_length,
                "problem": problem.row if problem and not unmarked else None,
                "modified": problem is None and value != default,
                "restart": not is_live(section, f.key) and f.key not in env,
                "locked": env.get(f.key, ""),
                "saved": bool(just_saved and f.key in just_saved),
                "default": ""
                if f.kind in ("secret", "mapping")
                else f"Default {f.describe(default)}"
                + (", from the configuration" if default != f.builtin else ""),
            }
            if row["locked"]:  # set by the environment: its value as text, no change here
                shown = ("Set" if value else "Not set") if f.kind == "secret" else f.describe(value)
                row.update(
                    kind="locked",
                    text=shown[0].upper() + shown[1:] if shown else shown,
                    default="",
                    modified=False,
                    hidden=f.key in saved_section,
                )
                group_rows.append(row)
                continue
            if f.kind == "secret":
                # The checkbox removes the value; where the dashboard's value hides the
                # configuration's, it brings the configuration's back.
                item = saved_section.get(f.key)
                if item is not None and value is None:
                    row["state"] = ("unset", "Removed here")
                elif value is None:
                    row["state"] = ("unset", "")
                elif item is not None:
                    row["state"] = ("set", f"Saved {date(item.saved_at)}")
                else:
                    row["state"] = ("set", "From the configuration")
                if item is not None and default is not None:
                    row["clear_label"] = "Use the configuration's"
                elif value is not None:
                    row["clear_label"] = "Remove"
                else:
                    row["clear_label"] = ""
            elif f.kind == "mapping":
                row["text"] = f.describe(value).capitalize()
            elif f.kind == "switch":
                row["checked"] = (form or {})[f.key] == "true" if sent(f.key) else bool(value)
            elif row["kind"] == "select":
                if f.meta.choices == "sources":
                    current = str(value or "")
                    extra = [current] if current and current not in addon_names else []
                    options = (
                        [("", "None")]
                        + [(name, name) for name in addon_names]
                        + [(name, f"{name} (not in the add-on list)") for name in extra]
                    )
                else:
                    options = f.options()
                row["options"] = options
                row["chosen"] = (form or {})[f.key] if sent(f.key) else f.show(value)
            else:
                row["text"] = (form or {})[f.key] if sent(f.key) else f.show(value)
            group_rows.append(row)
        result.append((group, _paired(members, group_rows)))
    return result


def _paired(members: list[SettingField], group_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of settings shown together (``Meta.pair``: two ends of a range) as one: the
    second one's row in the first's (``pair``), as long as neither is the environment's."""
    by_key = {row["name"]: row for row in group_rows}
    for f in members:
        first, second = by_key.get(f.key), by_key.get(f.meta.pair)
        if first is None or second is None or "locked" in (first["kind"], second["kind"]):
            continue
        group_rows.remove(second)
        configured = ", from the configuration"
        ends = [row["default"].removeprefix("Default ") for row in (first, second)]
        default = f" {f.meta.joiner} ".join(end.removesuffix(configured) for end in ends)
        first.update(
            pair=second,
            joiner=f.meta.joiner,
            modified=first["modified"] or second["modified"],
            default=f"Default {default}"
            + (configured if any(configured in e for e in ends) else ""),
        )
    return group_rows


Rows = list[tuple[Group, list[dict[str, Any]]]]


@dataclass(frozen=True)
class Fold:
    """The settings under a page's "More settings" (``Group.more``), collapsed unless one
    of them needs to be seen: it has a problem, was saved just now, or a notice links to
    it."""

    groups: Rows
    open: bool
    changed: int  # rows not at their default (a range's two ends: one row)


def folded(
    groups: Rows,
    *,
    problems: Mapping[str, Any] | None = None,
    linked: frozenset[str] = frozenset(),
    also: list[dict[str, Any]] | None = None,
) -> tuple[Rows, Fold]:
    """The page's groups shown first, and its fold. ``problems``: by setting key, also those
    a first step does not mark in their rows; ``linked``: row ids a notice links to;
    ``also``: rows of another form shown in the fold (the Catalog page's wait)."""
    shown = [(group, members) for group, members in groups if members and not group.more]
    more = [(group, members) for group, members in groups if members and group.more]
    rows = [row for _, members in more for row in members] + list(also or ())
    each = [part for row in rows for part in (row, row.get("pair")) if part]
    opened = any(
        part.get("problem")
        or part.get("saved")
        or part["name"] in (problems or {})
        or part["id"] in linked
        for part in each
    )
    return shown, Fold(more, opened, sum(1 for row in rows if row.get("modified")))


def _key_labels(section: str, text: str, kind: str) -> str:
    """A rule's message with setting names (``section.key`` or bare) replaced by the
    settings' labels."""
    labels = {f.key: f.meta.label.lower() for f in fields_of(section, kind)}
    return re.sub(rf"\b(?:{section}\.)?(\w+)\b", lambda m: labels.get(m.group(1), m.group(0)), text)


def _whole_problems(
    section: str,
    error: ValidationError,
    values: Mapping[str, Any],
    env: Mapping[str, str],
    kind: str,
) -> dict[str, Problem]:
    """Problems of the section as a whole (a rule across settings), on the first setting
    the rule names that can be changed here (not one the environment sets)."""
    known = {f.key: f for f in fields_of(section, kind) if f.editable and f.key not in env}
    if not known:  # everything is the environment's: the first setting says so
        known = {f.key: f for f in fields_of(section, kind) if f.editable}
    problems: dict[str, Problem] = {}
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        message = str(item["msg"]).removeprefix("Value error, ")
        loc = [str(part) for part in item["loc"]]
        if loc and loc[0] in known:
            key = loc[0]
            row = message[0].upper() + message[1:] + "."
            problems.setdefault(key, Problem(row, "is not accepted: " + message + "."))
            continue
        named = [key for key in re.findall(rf"\b{section}\.(\w+)", message) if key in known]
        key = named[0] if named else next(iter(known))
        meta = known[key].meta
        shown = {
            k: format_number(v) if isinstance(v, (int, float)) else v for k, v in values.items()
        }
        try:
            row = meta.cross.format(**shown) if meta.cross else ""
        except (KeyError, IndexError):
            row = ""
        text = _key_labels(section, message, kind)
        problems.setdefault(
            key,
            Problem(row or text[0].upper() + text[1:] + ".", meta.cross_notice or text + "."),
        )
    return problems


async def _catalog_rules(candidate: Any) -> dict[str, Problem]:
    """What building the catalog at startup needs (it would stop Shijhon otherwise)."""
    found = await anyio.to_thread.run_sync(catalog_problem, candidate)
    return {} if found is None else {found[0]: Problem(found[1], found[2])}


RULES = {"catalog": _catalog_rules}


async def submit(
    section: str,
    configured: Settings,
    saved: Mapping[str, Mapping[str, Saved]],
    form: Mapping[str, str],
    *,
    addon_names: list[str],
    locked: Locked,
) -> Submitted:
    """Parse and check a submitted page; nothing is written. Settings the environment sets
    are never taken from a form; a value saved for one can only be removed. A form that
    chooses another catalog is checked as that catalog's: its adapter's settings are
    taken only from a form that was made for it."""
    applying = getattr(effective(configured, saved, locked=locked)[0], section)
    result = Submitted({})
    kind = form_kind(section, applying, form, locked.get(section, {}))
    view = _view(section, configured, saved, locked, applying, kind)
    env = view.env
    for f in fields_of(section, kind):
        if f.key in env:
            if form.get(f.key + CLEAR) == "true" and f.key in view.saved:
                result.removals.append(f.key)
            continue
        if not f.editable or not _typed(section, f.key, form, kind, applying):
            continue
        if f.kind == "secret":
            typed = form.get(f.key, "").strip()
            if typed:
                result.values[f.key] = SecretStr(typed)
            elif form.get(f.key + CLEAR) == "true":
                # Remove the value: the dashboard's gives way to the configuration's, else
                # the configuration's is hidden.
                result.values[f.key] = view.default(f) if f.key in view.saved else None
            continue
        if f.key not in form:
            continue  # not on the page that was sent: kept as it is
        if f.kind == "switch":  # a checkbox after a hidden "false" of the same name
            result.values[f.key] = form[f.key] == "true"
            continue
        try:
            value = f.parse(form[f.key].lower() if f.meta.short else form[f.key])
        except Invalid as exc:
            result.problems[f.key] = Problem(str(exc), f.notice(str(exc)))
            continue
        current = view.value(f)
        if f.meta.choices == "sources" and value and value not in addon_names and value != current:
            result.problems[f.key] = Problem(
                "Choose one of the listed add-ons.", "must be one of the add-ons."
            )
            continue
        result.values[f.key] = value
    if result.problems:
        return result
    # The settings as they apply for this kind (another catalog than the one that applies:
    # none of that one's own settings), with what the form changes.
    known = {f.key for f in fields_of(section, kind)}
    values = {
        key: getattr(view.now, key)
        for key in (type(view.now).model_fields if view.now is not None else ())
        if key in known
    }
    values.update(result.values)
    try:  # (a value saved for the address before, not entered again with a new one: not kept)
        kept = [key for key in view.saved if key not in result.values]
        candidate, _ = in_place(getattr(configured, section), values, saved.get(section, {}), kept)
    except ValidationError as exc:
        result.problems.update(_whole_problems(section, exc, values, env, kind))
        result.first_step = _first_step(section, form, kind, applying, env, result.problems)
        return result
    if section == "catalog":  # what the catalog would get (as at startup)
        candidate = unbound(configured.catalog, candidate)
    result.candidate = candidate
    rule = RULES.get(section)
    if rule is not None:
        names = {f.key for f in fields_of(section, kind)}
        for key, found in (await rule(result.candidate)).items():
            # (A setting the adapter names without declaring it: said at its choice.)
            row = key if key in names else "kind"
            result.problems[row] = _environments(found, env.get(row))
    result.first_step = _first_step(section, form, kind, applying, env, result.problems)
    return result


def _first_step(
    section: str,
    form: Mapping[str, str],
    kind: str,
    applying: Any,
    env: Mapping[str, str],
    problems: Mapping[str, Problem],
) -> bool:
    """Whether a form that cannot be saved is the first of two steps (``Submitted``): it
    chose another catalog than the one whose settings it held, and every problem is on a
    setting of that catalog that can be entered on its page."""
    entered = {
        f.key
        for f in fields_of(section, kind)
        if f.editable and not _own(section, f.key) and f.key not in env
    }
    return (
        section == "catalog"
        and form.get(SHOWN_KIND, kind_of(section, applying)) != kind
        and bool(problems)
        and all(key in entered for key in problems)
    )


def _environments(problem: Problem, variable: str | None) -> Problem:
    """A problem on a setting the environment sets: to be fixed there."""
    if variable is None:
        return problem
    return Problem(
        f"The environment's value does not work: {problem.row[0].lower()}{problem.row[1:]}"
        f" Change {variable}.",
        f"is set by the environment ({variable}), and its value does not work.",
    )


def writes(
    section: str,
    configured: Settings,
    saved: Mapping[str, Mapping[str, Saved]],
    submitted: Submitted,
    *,
    locked: Locked,
) -> Written:
    """The saved values to write for a valid submission, by the precedence rule."""
    assert submitted.candidate is not None
    applying = getattr(effective(configured, saved, locked=locked)[0], section)
    kind = kind_of(section, submitted.candidate)
    view = _view(section, configured, saved, locked, applying, kind)
    fields = {f.key: f for f in fields_of(section, kind)}
    changed: list[str] = []
    to_write: dict[str, Any] = {}
    for key in submitted.values:
        wanted = getattr(submitted.candidate, key)
        # (With another catalog chosen, its settings change from what they would be.)
        if wanted != view.value(fields[key]):
            changed.append(key)
        item = view.saved.get(key)  # (what is saved for another catalog is not this one's)
        if wanted == view.default(fields[key]):
            if item is not None:
                to_write[key] = REMOVE
        elif item is None or encode(item.value) != encode(wanted):
            to_write[key] = wanted
    # Every field is on the page (secrets only when typed), so a section whose saved values
    # did not fit together at startup is written out whole here.
    for key in submitted.removals:  # values the environment hides, removed on request
        to_write[key] = REMOVE
    moved: list[tuple[str, str]] = []
    if section == "catalog":
        items = saved.get(section, {})
        # A value that belongs to an address, saved for the one before: removed unless it
        # was entered again with the new one (as ``submit`` left it out of the candidate).
        adapter = plugin.find(kind)
        bound = adapter.bound if adapter is not None else {}
        kept = [key for key in view.saved if key not in submitted.values]
        for key in displaced(submitted.candidate, items, kept):
            to_write[key] = REMOVE
            moved.append((key, bound[key]))
            if key not in changed and getattr(submitted.candidate, key) != view.value(fields[key]):
                changed.append(key)
        # (Those held back already - their address changed outside the dashboard - go with
        # this save, ``_adapter_writes``: said too, unless entered again now.)
        moved += [
            (key, bound[key])
            for key in saved_for(items, kind)
            if key in bound and key not in view.saved and key not in to_write
        ]
        to_write.update(_adapter_writes(kind, items, view.saved, to_write))
        to_write.update(_bound_writes(submitted.candidate, bound, items, view.saved, to_write))
    return Written(changed, to_write, list(submitted.removals), moved)


def _bound_writes(
    candidate: Any,
    bound: Mapping[str, str],
    saved: Mapping[str, Saved],
    ours: Mapping[str, Saved],
    to_write: Mapping[str, Any],
) -> dict[str, Any]:
    """The places of the saved values that belong to an address (``saved.BOUND``), as they
    are once ``to_write`` is written: each value saved now, or kept, is the place's that its
    address has in ``candidate``."""
    left = {key for key in ours if key in bound} | {key for key in to_write if key in bound}
    values = {key: to_write[key] if key in to_write else ours[key].value for key in left}
    marks = {
        key: place(getattr(candidate, bound[key], None))
        for key in sorted(left)
        if values[key] is not REMOVE and values[key] is not None  # (a removal has no place)
    }
    record = saved.get(BOUND)
    if marks and (record is None or record.value != marks):
        return {BOUND: marks}
    return {BOUND: REMOVE} if not marks and record is not None else {}


def _adapter_writes(
    kind: str, saved: Mapping[str, Saved], ours: Mapping[str, Saved], to_write: Mapping[str, Any]
) -> dict[str, Any]:
    """What a catalog save writes besides its values. The values saved for another
    catalog's own settings (``saved`` beyond ``ours``, this kind's) are removed - a token
    saved for one catalog is never another's, whether the dashboard or the configuration
    chose the other - and the kind the saved adapter settings are for is recorded with them
    (``saved.ADAPTER``)."""
    more: dict[str, Any] = {}
    theirs = [key for key in saved if key not in CORE_CATALOG_KEYS and key not in RECORDS]
    more.update({key: REMOVE for key in theirs if key not in ours and key not in to_write})
    left = [key for key in theirs if {**to_write, **more}.get(key) is not REMOVE]
    left += [
        key
        for key, value in to_write.items()
        if key not in CORE_CATALOG_KEYS and value is not REMOVE and key not in left
    ]
    marked = saved.get(ADAPTER)
    if left and (marked is None or marked.value != kind):
        more[ADAPTER] = kind
    elif not left and marked is not None:
        more[ADAPTER] = REMOVE
    return more
