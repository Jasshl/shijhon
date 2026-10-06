# Contributing to Shijhon

Shijhon is a beta, written by one person. Bug reports, findings from real apps and patches
are welcome; so are questions before you start on something large.

## Before you write code

- Read the [README](README.md) and [docs/development.md](docs/development.md): the setup,
  the tests, a map of the source.
- Look at [docs/known-issues.md](docs/known-issues.md) and
  [docs/reference/known-gaps.md](docs/reference/known-gaps.md): what you found may be
  recorded there.
- Discuss a change to established behavior in an issue before you build it.

## The rules that matter most

**1. Apps are built for a local Navidrome.** There, answers are stable and requests are
cheap and fast. Changes keep it that way:

- *Stable answers*: Navidrome's IDs and its metadata of owned music stay as they are, and
  an item looks the same from its first answer on.
- *Bounded catalog work*: a view waits for the catalog for a limited time, then
  answers without it. Work at add-ons happens on actions and plays, cached and paced.
- **No behavior depends on an app's name or user agent.** The client name is only a key
  for per-device state. A heuristic found with one app is a general rule, with settings,
  that falls back to plain Navidrome behavior. An app that does not get on with Shijhon
  is fixed through the OpenSubsonic specification and Navidrome parity, never by handling
  that app specially.

**2. Navidrome stays the authority, unmodified.** Users, the library, IDs, playlists,
favorites, scrobbling and the metadata of owned music are Navidrome's. Shijhon does not
rewrite Navidrome's answers about owned items: it adds catalog data and serves the audio
of placeholder tracks. (One exception: a partly owned album that is shown complete carries
the complete album's song count and length.) Credentials are checked with Navidrome before
Shijhon does any work for a request.

**3. Providers stay outside.** Code for a specific catalog or add-on provider does not
go into this repository: no code, comment, test or fixture, and no token or token service.
What is specific to one catalog belongs in a catalog adapter, a separate package
([docs/development.md](docs/development.md#writing-a-catalog-adapter)); what is specific
to one audio source belongs in an add-on.

**4. Nothing private in the repository.** Fixtures use invented artists, titles, IDs and
ISRCs. No real catalog answers, raw or sanitized; no personal music, accounts or
listening history; no add-on addresses, credentials or tokens; no configuration from a
real installation. Logs and error messages never carry a credential, a token or a full
address: keep it that way in new code.

**5. Mature libraries before custom code.** A custom subsystem needs a reason.

**6. Library writes are the dangerous part.** Anything that writes into the library or
recovers such a write (the placeholder engine, fills, the cleanup, migrations) must leave
the folder, Shijhon's database and Navidrome in step when it is stopped at any point.
`tests/acceptance/test_R_stops.py` tests that: add to it.

## Tests

The tests run against a **real, disposable Navidrome** and synthetic audio. Nothing is
mocked where Navidrome's own behavior is the question.

```sh
uv sync
uv run pytest -n auto        # every default suite, in parallel
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- Run the whole suite, the linters and the type check before you send a change
  ([docs/development.md](docs/development.md#checks) says what can fail because of the
  machine).
- A change in behavior comes with a test.
- **Server-side answers do not establish what an app does.** A change to what apps see
  needs a check with real apps
  ([docs/development.md](docs/development.md#checks-with-real-apps)); say in your change
  which apps you tried, and with which versions.
- A change to how Shijhon reads or follows Navidrome should pass the canary suite
  (`uv run pytest -m canary`).

## Sending a change

- One change per pull request, with the reason in its description. Update the documents
  that describe what you changed.
- A schema change is a new numbered migration in `src/shijhon/store/migrations/`; applied
  migrations are never edited in a way that changes what they do. Say in the change what
  a running installation has to do, and how to go back.
- Sign your commits off (`git commit -s`): it states that you wrote the change or may pass
  it on under the project's license (the
  [Developer Certificate of Origin](https://developercertificate.org/)).

## License

Shijhon is under the GNU Affero General Public License, version 3 or later
([LICENSE](LICENSE), [NOTICE](NOTICE)). Contributions are accepted under the same terms,
including the additional permission for catalog adapters.

## Security

Please report a security problem privately first - through the repository's private
vulnerability reporting ("Report a vulnerability" under its Security tab) - rather than in
a public issue.
