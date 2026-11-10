-- An add-on's step time and its own time part: the plan's step time (``step_seconds``) is
-- when the next step's add-ons join a play's start, and ``budget_seconds`` is the add-on's
-- time when nobody waits (downloads, songs prepared ahead, fetches ahead), one add-on at a
-- time. Both were one value: the plan keeps the times it had. ``slow_after``: its own
-- "slow after" (times its usual time to first audio; NULL: the installation's).
ALTER TABLE sources ADD COLUMN step_seconds REAL;
ALTER TABLE sources ADD COLUMN slow_after REAL;
UPDATE sources SET step_seconds = budget_seconds;
