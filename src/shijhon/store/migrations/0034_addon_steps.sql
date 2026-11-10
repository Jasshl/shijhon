-- The add-ons' plan: each add-on's step (add-ons with one step start together; among the
-- add-ons asked for audio, in the list's order). An add-on's budget_seconds is its step's
-- time. The routing modes are gone, converted once: every add-on starts as a step of its
-- own in the list's order; under "primary_first" the primary add-on comes first and an
-- add-on whose check says it has the song ready takes over after the primary's time, as
-- it did; under "ready_first" such an add-on goes first. The primary's timeouts and misses
-- now count for every add-on.
ALTER TABLE sources ADD COLUMN step INTEGER NOT NULL DEFAULT 0;
UPDATE sources SET step = (
    SELECT COUNT(*) FROM sources AS earlier
    WHERE earlier.position < sources.position
       OR (earlier.position = sources.position AND earlier.id <= sources.id)
);
UPDATE sources SET position = (SELECT MIN(position) FROM sources) - 1, step = 0
WHERE name = (
    SELECT json_extract(value, '$') FROM saved_settings
    WHERE section = 'delivery' AND key = 'primary_source'
) AND EXISTS (
    SELECT 1 FROM saved_settings
    WHERE section = 'delivery' AND key = 'routing' AND value = '"primary_first"'
);
INSERT OR IGNORE INTO saved_settings (section, key, value, saved_at, saved_by)
SELECT 'delivery', 'ready_checks', 'true', saved_at, saved_by FROM saved_settings
WHERE section = 'delivery' AND key = 'routing' AND value IN ('"primary_first"', '"ready_first"');
INSERT OR IGNORE INTO saved_settings (section, key, value, saved_at, saved_by)
SELECT 'delivery', 'ready_handover_seconds',
    CASE WHEN value = '"ready_first"' THEN '0' ELSE COALESCE((
        SELECT budget.value FROM saved_settings AS budget
        WHERE budget.section = 'delivery' AND budget.key = 'primary_budget_seconds'
    ), '4.5') END,
    saved_at, saved_by
FROM saved_settings
WHERE section = 'delivery' AND key = 'routing' AND value IN ('"primary_first"', '"ready_first"');
DELETE FROM saved_settings WHERE section = 'delivery' AND key IN (
    'routing', 'primary_source', 'reliable_source', 'primary_budget_seconds',
    'reliable_lookup_after_seconds', 'max_attempts', 'primary_cooldown_switches',
    'primary_release_miss_minutes'
);
UPDATE saved_settings SET key = 'cooldown_timeouts'
    WHERE section = 'delivery' AND key = 'primary_cooldown_timeouts';
UPDATE saved_settings SET key = 'miss_hours'
    WHERE section = 'delivery' AND key = 'primary_miss_hours';
