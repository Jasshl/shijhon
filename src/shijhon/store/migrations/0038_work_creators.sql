-- Who made a discovery mix or a playlist import, as ``playlist_requests.creator`` says it
-- for a described playlist, so that revoking the agent that made one stops its work:
-- its runs, refreshes and publications are checked against that agent's grant (or the
-- grant of whoever acted on a run since: the run's ``actor``). ``creator``: the agent's
-- grant (``playlist_agent_tokens.id``), '' for the owner on the dashboard, NULL for one
-- made before this was kept (which runs as the account's, as it did).
ALTER TABLE playlist_discovery_requests ADD COLUMN creator TEXT;
ALTER TABLE playlist_imports ADD COLUMN creator TEXT;
