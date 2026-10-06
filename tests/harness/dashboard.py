"""A browser for the dashboard: cookies, sign-in, and forms filled from the page itself."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import httpx

PATH = "/shijhon"


class _Forms(HTMLParser):
    """The fields of every form on a page, by the form's action: what a browser would send
    (text and password fields, checked boxes, selected options, hidden fields)."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, dict[str, str]] = {}
        self._action: str | None = None
        self._by_id: dict[str, str] = {}
        self._select: tuple[str, str] | None = None  # (form action, name)
        self._first_option: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._action = a.get("action", "")
            self.forms.setdefault(self._action, {})
            if "id" in a:
                self._by_id[a["id"]] = self._action
            return
        action = self._by_id.get(a.get("form", ""), self._action)
        if action is None:
            return
        fields = self.forms.setdefault(action, {})
        if tag == "input" and "name" in a:
            kind = a.get("type", "text")
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    fields[a["name"]] = a.get("value", "on")
            else:
                fields[a["name"]] = a.get("value", "")
        elif tag == "select" and "name" in a:
            self._select = (action, a["name"])
            self._first_option = True
        elif tag == "option" and self._select is not None:
            form, name = self._select
            if self._first_option or "selected" in a:
                self.forms[form][name] = a.get("value", "")
            self._first_option = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._action = None
        elif tag == "select":
            self._select = None


def forms(html: str) -> dict[str, dict[str, str]]:
    parser = _Forms()
    parser.feed(html)
    return parser.forms


class Browser:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, follow_redirects=False, timeout=60)
        self.seen: list[httpx.Response] = []  # every response, for checks over all of them

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        response = self.http.get(path if path.startswith("/") else f"{PATH}/{path}", **kwargs)
        self.seen.append(response)
        return response

    def post(self, path: str, data: dict[str, str], **kwargs: Any) -> httpx.Response:
        response = self.http.post(
            path if path.startswith("/") else f"{PATH}/{path}", data=data, **kwargs
        )
        self.seen.append(response)
        return response

    def sign_in(self, user: str, password: str, *, next: str = "") -> httpx.Response:
        page = self.get("sign-in")
        found = re.search(r'name="signin" value="([^"]+)"', page.text)
        assert found, page.text
        return self.post(
            "sign-in",
            {"signin": found.group(1), "username": user, "password": password, "next": next},
        )

    def form(self, page: str, action: str | None = None) -> dict[str, str]:
        """The fields of the page's form posting to ``action`` (default: the page itself)."""
        response = self.get(page)
        assert response.status_code == 200, response.text
        return forms(response.text)[f"{PATH}/{action or page}"]

    def submit(
        self, page: str, changes: dict[str, str], action: str | None = None
    ) -> httpx.Response:
        """Fill the page's form as shown, change some fields, and send it."""
        data = self.form(page, action)
        data.update(changes)
        return self.post(action or page, data)

    def csrf(self) -> str:
        found = re.search(r'name="csrf" value="([^"]+)"', self.get("appearance").text)
        assert found
        return found.group(1)
