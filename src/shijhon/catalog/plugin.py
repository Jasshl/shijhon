"""Catalog adapters as plugins.

Shijhon itself knows no catalog by name. ``[catalog] kind = "<name>"`` selects an adapter
that the installation provides: a Python package that registers one entry point in the group
``shijhon.catalogs``. The entry point's name is the ``kind``; its value is an
:class:`Adapter`::

    [project.entry-points."shijhon.catalogs"]
    example = "shijhon_catalog_example:adapter"

An adapter declares its own settings (a pydantic model: names, types and limits; a
``SecretStr`` is a secret) and how its catalog is built from them. Its settings live in
the ``[catalog]`` section next to Shijhon's own (``config.catalog_settings``), so they
are read from the configuration file and the environment, checked, saved and locked like
any other setting, and the dashboard's Catalog page shows them from the declaration.
They are that adapter's alone: with another kind in use, none of them is read, applied or
shown. Whose a setting is, is always said next to it: in the file by the file's ``kind``
(the environment's settings are the configured kind's), in the dashboard's saved values by
the kind recorded with them.

What an adapter may use of Shijhon: ``shijhon.catalog.base`` (the interface and its
error), ``shijhon.catalog.model`` (the data), this module (also ``denied``: whether an
HTTP error is the network policy's refusal), and ``shijhon.catalog.contract`` (checks any
adapter can run against the interface). An adapter's package states the Shijhon versions it
works with in its own dependencies.

Messages an adapter writes - a ``Problem``'s words, a validator's error, a ``ValueError``
from ``build`` - are shown and logged: they never hold a setting's value.
"""

from __future__ import annotations

import logging
import re
import threading
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, SecretStr

from shijhon.catalog.base import Catalog
from shijhon.delivery.netpolicy import Reach, Resolver, denied, policy_client, system_resolver

__all__ = [
    "GROUP",
    "Adapter",
    "Context",
    "Problem",
    "Words",
    "adapter",
    "denied",
    "installed",
    "register",
    "unregister",
    "validate",
]

log = logging.getLogger(__name__)
GROUP = "shijhon.catalogs"
NONE = "none"  # the kind without a catalog
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


@dataclass(frozen=True)
class Words:
    """A setting's words on the dashboard's Catalog page."""

    label: str
    help: str = ""
    group: str = "connection"  # "connection", "albums" or "advanced" (under "More settings")
    unit: str | None = None  # None: from the key's suffix (_seconds: "s")
    spoken: str | None = None  # the unit for screen readers ("seconds")
    options: Mapping[str, str] = field(default_factory=dict)  # a choice's labels
    short: bool = False  # a short code (a region): a narrow field, lower case
    max_length: int | None = None  # a text's length at most (the field takes no more)
    pattern: str | None = None  # a text's extra check (a regular expression, whole value)
    pattern_error: str = ""  # what to enter when it fails
    # "sources": a text that names one of the installation's add-ons - the page offers
    # them as a choice.
    choices: str | None = None


@dataclass(frozen=True)
class Problem:
    """What keeps a catalog from being built with these settings (Shijhon would not start
    with them): the setting to change, what to enter (its row on the dashboard), and the
    rule in words that follow the setting's label in a notice ("… is needed to …")."""

    setting: str
    row: str
    notice: str


@dataclass(frozen=True)
class Context:
    """What Shijhon gives an adapter to build its catalog with."""

    timeout_seconds: float = 15.0  # ``[catalog] timeout_seconds``
    resolver: Resolver = system_resolver
    # The installation's add-ons (``delivery.sources.SourceRegistry``): for Shijhon's own
    # kind that takes its catalog from one of them (``catalog.addon``), and no
    # part of what an adapter may use. None where there are none (a tool, a test).
    addons: Any = field(default=None, repr=False, compare=False)

    def http(self) -> httpx.AsyncClient:
        """An HTTP client under Shijhon's network policy: public addresses only (private,
        loopback and link-local destinations are refused, also behind DNS names and
        redirects). The catalog owns it and closes it in ``aclose``."""
        return policy_client(
            Reach.PUBLIC, timeout=httpx.Timeout(self.timeout_seconds), resolver=self.resolver
        )


@dataclass(frozen=True, eq=False)
class Adapter:
    """One catalog adapter, as its package declares it.

    ``build(settings, context)`` returns the catalog; ``settings`` is the ``[catalog]``
    section, holding Shijhon's own settings and this adapter's (an instance of
    ``settings``' class too). It may raise ``ValueError`` with a short reason (never a
    value): Shijhon then does not start.
    """

    label: str  # shown on the dashboard, e.g. "Example Music"
    build: Callable[[Any, Context], Catalog]
    # The adapter's own settings. Names must differ from Shijhon's own ``[catalog]``
    # settings; a secret is a ``SecretStr``; a setting that may be absent is optional
    # (``None`` by default).
    settings: type[BaseModel] | None = None
    words: Mapping[str, Words] = field(default_factory=dict)  # in the page's order
    # Settings that are one choice (the first of them that is set is the one used, e.g. a
    # token, or a file holding it, or a service issuing it; each optional): the environment
    # giving one of them replaces the others, and the dashboard shows them locked together.
    one_choice: tuple[tuple[str, ...], ...] = ()
    # Settings that belong to the address another setting names (e.g. the headers sent to a
    # service, often a key): ``{"service_headers": "service_url"}``. A value from the
    # configuration file or the environment is not sent to an address saved in the
    # dashboard on another host; a secret saved in the dashboard goes only to the host its
    # address had when it was saved (declare a credential a ``SecretStr``: a setting the
    # page shows is sent again with every form, with the address that form holds). An
    # address is not itself bound.
    bound: Mapping[str, str] = field(default_factory=dict)
    # What would keep ``build`` from working, found without building (it may read files);
    # None when nothing would. The dashboard refuses to save such settings.
    problem: Callable[[Any], Problem | None] | None = None
    notice: str = ""  # said on the Catalog page and at startup while this adapter is in use


class AdapterError(ValueError):
    """An adapter that cannot be used; the message names it, never a setting's value."""


_registered: dict[str, Adapter] = {}  # given in-process: tests, an embedding application
_loaded: dict[str, Adapter] | None = None  # from the entry points, read once
_broken: dict[str, str] = {}  # entry point name -> why it could not be loaded
_loading = threading.Lock()


def register(name: str, adapter: Adapter) -> None:
    """Make ``adapter`` available as ``kind = "<name>"`` in this process, without an entry
    point (tests, an application embedding Shijhon). It goes before an installed adapter of
    that name."""
    _registered[name] = validate(name, adapter)
    _changed()


def unregister(name: str) -> None:
    _registered.pop(name, None)
    _changed()


def _changed() -> None:
    from shijhon import config  # the settings classes made for the adapters before

    config.forget_adapters()


def _check_name(name: str) -> None:
    if name == NONE or not _NAME.fullmatch(name):
        raise AdapterError(
            f"catalog adapter name {name!r}: lower-case letters, digits, '-' and '_',"
            " starting with a letter (and not 'none')"
        )


def plain(annotation: Any) -> Any:
    """A setting's type without its ``| None``."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        rest = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return rest[0] if len(rest) == 1 else annotation
    return annotation


def supported(annotation: Any) -> bool:
    """A setting's type the configuration and the dashboard's Catalog page can hold:
    ``str``, ``int``, ``float``, ``bool``, a ``Literal`` of strings, ``SecretStr``, ``Path``
    (each may be optional), ``list[int]``, and ``dict[str, SecretStr]`` or ``dict[str,
    str]`` (set in the configuration file, shown as a count)."""
    kind = plain(annotation)
    origin = typing.get_origin(kind)
    if kind in (str, int, float, bool, SecretStr, Path):
        return True
    if origin is Literal:
        return all(isinstance(value, str) for value in typing.get_args(kind))
    if origin is list:
        return typing.get_args(kind) == (int,)
    if origin is dict:
        return typing.get_args(kind) in ((str, SecretStr), (str, str))
    return False


def validate(name: str, adapter: object) -> Adapter:
    """The declaration checked as the loader checks it; raises :class:`AdapterError`."""
    _check_name(name)
    return _checked(name, adapter)


def _checked(name: str, adapter: object) -> Adapter:
    from shijhon.config import CORE_CATALOG_KEYS  # Shijhon's own [catalog] settings

    if not isinstance(adapter, Adapter):
        raise AdapterError(f"catalog adapter {name!r} is not a shijhon.catalog.plugin.Adapter")
    model = adapter.settings
    if model is not None and not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise AdapterError(f"catalog adapter {name!r}: its settings are not a pydantic model")
    fields = model.model_fields if model is not None else {}
    clash = sorted(CORE_CATALOG_KEYS & set(fields))
    if clash:
        raise AdapterError(
            f"catalog adapter {name!r} declares settings Shijhon has itself: {', '.join(clash)}"
        )
    for key, info in fields.items():
        if not supported(info.annotation):
            raise AdapterError(
                f"catalog adapter {name!r}: {key} has a type the configuration cannot hold"
                " (str, int, float, bool, a Literal, SecretStr, Path, list[int],"
                " dict[str, SecretStr])"
            )
        if info.is_required():
            raise AdapterError(
                f"catalog adapter {name!r}: {key} needs a default (what is missing is said"
                " by problem())"
            )
    named = (
        set(adapter.words)
        | {key for group in adapter.one_choice for key in group}
        | set(adapter.bound)
        | set(adapter.bound.values())
    )
    unknown = sorted(named - set(fields))
    if unknown:
        raise AdapterError(
            f"catalog adapter {name!r} names settings it does not declare: {', '.join(unknown)}"
        )
    for key, words in adapter.words.items():
        if words.choices is not None and (
            words.choices != "sources" or plain(fields[key].annotation) is not str
        ):
            raise AdapterError(
                f"catalog adapter {name!r}: {key} offers choices the page does not have"
                ' ("sources", for a text)'
            )
    for key in {key for group in adapter.one_choice for key in group}:
        if fields[key].get_default(call_default_factory=True) is not None:
            raise AdapterError(
                f"catalog adapter {name!r}: {key} is one of a choice, so it must be optional"
                " (None by default)"
            )
    for key, address in adapter.bound.items():
        if plain(fields[address].annotation) not in (str, SecretStr):
            raise AdapterError(f"catalog adapter {name!r}: {key} is bound to no address")
        if address in adapter.bound:
            # (What belongs to an address goes or stays with that address alone: were the
            # address itself held back with another one, its settings would follow whatever
            # address took its place.)
            raise AdapterError(
                f"catalog adapter {name!r}: {key} is bound to {address}, which is itself"
                " bound to an address (bind both to that one)"
            )
    return adapter


def _load() -> dict[str, Adapter]:
    global _loaded
    with _loading:  # (also asked from worker threads: read once)
        if _loaded is None:
            _loaded = _read_entry_points()
    return _loaded


def _read_entry_points() -> dict[str, Adapter]:
    found: dict[str, Adapter] = {}
    for entry in entry_points(group=GROUP):
        try:
            if entry.name in found or entry.name in _broken:
                # Which of them is meant cannot be told: neither is used.
                found.pop(entry.name, None)
                raise AdapterError("two installed packages register this name")
            found[entry.name] = validate(entry.name, entry.load())
        except Exception as exc:  # a broken adapter must not take the others down
            reason = str(exc) if isinstance(exc, AdapterError) else type(exc).__name__
            _broken[entry.name] = reason
            log.error("catalog adapter %r could not be loaded: %s", entry.name, reason)
    return found


def installed() -> dict[str, Adapter]:
    """The adapters this installation has, by ``kind``."""
    return {**_load(), **_registered}


def broken() -> list[tuple[str, str]]:
    """The installed adapters that could not be loaded, with why."""
    _load()
    return sorted((name, why) for name, why in _broken.items() if name not in _registered)


def find(kind: str) -> Adapter | None:
    return installed().get(kind)


def adapter(kind: str) -> Adapter:
    """The adapter named ``kind``; raises :class:`AdapterError` saying what is installed."""
    found = find(kind)
    if found is not None:
        return found
    if kind in _broken:  # (read with the entry points, above)
        raise AdapterError(f"the catalog adapter {kind!r} could not be loaded: {_broken[kind]}")
    names = ", ".join(sorted(installed())) or "none"
    raise AdapterError(
        f"no catalog adapter named {kind!r} is installed (installed: {names}). An adapter"
        " is a separate package: install it into Shijhon's environment or image"
    )
