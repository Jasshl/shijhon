# Personal access contract

Navidrome is the identity and library-permission authority. Shijhon keeps no second
password store. Personal records belong to immutable Navidrome user IDs, never names.

`shijhon.access.Principal` is immutable and contains `user_id: str`, canonical
`username: str`, `is_admin: bool`, and current `library_ids: frozenset[int]`.
`principal.can_access(library_id)` tests a native library grant; admin status never
substitutes for a grant.

`AccessService(navidrome).verify(username, *, expected_user_id=None)` resolves an
**already authenticated** caller through Navidrome's native user and user-library
endpoints. It does not authenticate credentials. Durable sessions, connections and
jobs must pass their recorded `expected_user_id`; an empty expected ID is refused.
`personal(username, *, expected_user_id)` also requires the configured Shijhon library.
`AccessDenied` means identity/access changed; `AccessUnavailable` means no trustworthy
current verdict. Neither an unknown verdict nor an old admin answer permits work.

`verify_native(token)` checks an incoming native bearer against Navidrome's protected
`GET /api/user/{id}` self-read and revalidates current identity/grants. The decoded JWT
immutable `uid` only locates the user. Its claims never authenticate the request.
Tokens without the pinned native API's `uid`/`sub` are refused. The original bearer
goes to upstream using a cookie-free client before service-account identity work;
no service token replaces caller authentication. Responses must identify exactly
the located user and name. Bearers are not retained by this service.

REST adapters call `verify(caller.username)` only after the existing credential checker
accepts native credentials. Collection adapters convert integer native library IDs to
their string IDs and intersect these grants with collection predicates before ordering,
counting or pagination. This module does not change collection membership or filter
music. Saved Subsonic authorization supports owner actions. Structural collection
scans use the caller's native bearer from a successful password sign-in or verified
native request, with stable ID ordering and a physical-library constraint. These
bearers stay in bounded memory for at most one hour, never in SQLite or logs; a restart
drops them. The last complete structural index and personal selections survive.
Service-account credentials never fetch collection song pages or personal artwork.

Dashboard adapters use `Dashboard._personal_page(handler, *, form_reader=None)` for
own-account pages. The callback receives `Session.user_id` and the freshly checked
`Session.principal`. The wrapper verifies identity/library access before reading forms
and preserves session CSRF checks. `Dashboard._page` additionally requires current
administrator rights for installation pages. A reused username cannot inherit an old
session, playlist connection, job or personal setting.
