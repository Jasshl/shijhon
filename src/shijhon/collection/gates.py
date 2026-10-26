"""Require the configured physical-library grant before any Shijhon music work."""

from __future__ import annotations

from shijhon.access import AccessDenied, AccessService, AccessUnavailable
from shijhon.proxy.app import RequestContext
from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.params import RestCall, folders_read


class MusicGate:
    def __init__(self, access: AccessService, library_id: int) -> None:
        self.access, self.library_id = access, library_id

    async def __call__(self, call: RestCall, ctx: RequestContext) -> bool:
        # RequestContext invokes this only after accepting the request credentials.
        # Its reentrant caller() supplies that proof without running the gate again.
        caller = await ctx.caller()
        if caller is None:
            return False
        # Only where Navidrome reads that parameter, as it reads it: on a stream or an
        # album page it ignores it, and a native answer would play a placeholder's silence.
        folders = folders_read(call)
        if folders and str(self.library_id) not in folders:
            return False
        try:
            principal = await self.access.verify(caller.username)
        except AccessDenied:
            return False
        except AccessUnavailable:
            raise CheckFailed("current library access unavailable") from None
        ctx.kept["shijhon.principal"] = principal
        if not set(folders) <= {str(i) for i in principal.library_ids}:
            # One the caller cannot read: Navidrome's own answer (its error).
            return False
        return principal.can_access(self.library_id)
