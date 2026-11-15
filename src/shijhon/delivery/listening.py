"""What each user is listening to, as far as Shijhon can tell: the play queue a client
saved, "now playing" reports, and the plays Shijhon started from add-ons. Warm-ahead uses
it to follow the client's own queue, to warm ahead only for the song being played, and to
leave alone a client that fetches upcoming songs itself.

Songs are identified by a key that stays the same across a commit: the catalog track
(``demo:123``) for catalog songs and placeholders, else the native song ID. A listener
is a user and the client name it sends; queues and reports are per user, as Navidrome
keeps them.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

Listener = tuple[str, str]  # (user, client name)
MAX_STARTS = 64  # remembered play starts per listener
QUEUE_SECONDS = 2 * 3600.0  # a saved queue is followed this long


class Listening:
    def __init__(
        self,
        *,
        memory_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.memory = memory_seconds
        self.clock = clock
        # user -> (the keys of the saved queue, its current position, when saved)
        self._queues: dict[str, tuple[list[str], int, float]] = {}
        self._playing: dict[str, tuple[str, float]] = {}  # user -> (key, when reported)
        # What each client said it plays (a report, a saved queue's current song): (key,
        # when). The account's reports and queues are any of its devices'.
        self._said: dict[Listener, tuple[str, float]] = {}
        self._starts: dict[Listener, deque[tuple[float, str]]] = {}
        self._prefetching: dict[Listener, float] = {}  # listener -> until
        # Told when a user's client says which song it plays now (a report, a saved queue's
        # current song): a fetch of it waiting for its turn goes at once.
        self.on_current: Callable[[str, str], None] | None = None

    def clear(self) -> None:
        """Forget everything (tests)."""
        self._queues.clear()
        self._playing.clear()
        self._said.clear()
        self._starts.clear()
        self._prefetching.clear()

    # --- what clients tell ------------------------------------------------------------------

    def saved_queue(
        self, user: str, keys: list[str], current: int | None = None, client: str | None = None
    ) -> None:
        self._queues[user] = (list(keys), current or 0, self.clock())
        if client is not None and 0 <= (current or 0) < len(keys):
            self._say((user, client), keys[current or 0])
        if self.on_current is not None and 0 <= (current or 0) < len(keys):
            self.on_current(user, keys[current or 0])

    def reported(self, user: str, key: str, client: str | None = None) -> None:
        """The user's client reports ``key`` as playing now."""
        self._playing[user] = (key, self.clock())
        if client is not None:
            self._say((user, client), key)
        if self.on_current is not None:
            self.on_current(user, key)

    def _say(self, who: Listener, key: str) -> None:
        if len(self._said) > 1024:  # forget clients that went quiet
            recent = self.clock() - self.memory
            self._said = {w: said for w, said in self._said.items() if said[1] >= recent}
        self._said[who] = (key, self.clock())

    def started(self, who: Listener, key: str, ago: float = 0.0) -> float:
        """A play began at byte zero (its request came ``ago`` seconds before); returns
        when."""
        now = self.clock()
        if len(self._starts) > 1024:  # forget listeners that went quiet
            recent = now - self.memory
            self._starts = {  # (backdated starts come in any order: the newest of each)
                w: d for w, d in self._starts.items() if d and max(at for at, _ in d) >= recent
            }
            self._prefetching = {w: t for w, t in self._prefetching.items() if t > now}
        starts = self._starts.setdefault(who, deque(maxlen=MAX_STARTS))
        at = now - max(0.0, ago)
        starts.append((at, key))
        return at

    # --- what warm-ahead asks -----------------------------------------------------------------

    def latest(self, user: str) -> tuple[str, float] | None:
        """The song the user's client last reported as playing, and when (recently)."""
        found = self._playing.get(user)
        if found is None or self.clock() - found[1] > self.memory:
            return None
        return found

    def current_for(self, user: str) -> str | None:
        """The song the user's client says it plays now: its latest "now playing" report or
        its saved queue's current song, whichever came later (recently)."""
        said: list[tuple[float, str]] = []
        report = self._playing.get(user)
        if report is not None:
            said.append((report[1], report[0]))
        queue = self._queues.get(user)
        if queue is not None and 0 <= queue[1] < len(queue[0]):
            said.append((queue[2], queue[0][queue[1]]))
        if not said:
            return None
        at, key = max(said)
        return key if self.clock() - at <= self.memory else None

    def current_for_client(self, who: Listener) -> str | None:
        """The song this client itself said it plays now (its latest report or saved
        queue), recently; None when it said nothing."""
        said = self._said.get(who)
        if said is None or self.clock() - said[1] > self.memory:
            return None
        return said[0]

    def next_in_queue(self, user: str, key: str, depth: int) -> list[str] | None:
        """The ``depth`` songs after ``key`` in the user's saved queue (from its current
        position on, when the song is repeated); None when no recent queue holds it."""
        saved = self._queues.get(user)
        if saved is None or self.clock() - saved[2] > QUEUE_SECONDS:
            return None
        queue, current, _ = saved
        places = [i for i, k in enumerate(queue) if k == key]
        if not places:
            return None
        index = next((i for i in places if i >= current), places[0])
        return queue[index + 1 : index + 1 + depth]

    def fetched_between(
        self, who: Listener, since: float, until: float, excluding: set[str]
    ) -> set[str]:
        """Other songs this client started to fetch in that time."""
        return {
            k for at, k in self._starts.get(who, ()) if since <= at <= until and k not in excluding
        }

    def prefetching(self, who: Listener) -> bool:
        return self._prefetching.get(who, 0.0) > self.clock()

    def mark_prefetching(self, who: Listener) -> bool:
        """The client fetches upcoming songs itself; True when this is news."""
        news = not self.prefetching(who)
        self._prefetching[who] = self.clock() + self.memory
        return news
