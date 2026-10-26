-- Each account's three built-in libraries (``collection/policy.py``):
-- Original imported library: its imports, the albums of the playlists it imported and its
--   favorites;
-- Shijhon library: the catalog albums Shijhon added because of it (what its apps played,
--   favorited, rated, put into a playlist, downloaded, shared or bookmarked, the audio it
--   prepared, the playlists Shijhon published for it);
-- All: the two together.
-- Each keeps its own hand additions and removals: a membership profile of its own.

-- 1 when the account's activity recorded the song (an app's action, a publication, a
-- preparation), 0 when only a choice on the Collection page or the reviewed library setup
-- did. Only activity brings albums into the Shijhon library.
ALTER TABLE collection_history ADD COLUMN activity INTEGER NOT NULL DEFAULT 0
    CHECK(activity IN (0,1));
-- Rows recorded before tell their source by their time: the backfill of migration 0016
-- (added_at 0), a Save on the Collection page (its own row, at or after its approval)
-- and the reviewed setup (at its application). Every other row came from activity. The
-- Saves are read once, grouped by song (each song's first approved Save).
CREATE TABLE collection_upgrade_saves AS
SELECT o.owner_id,o.library_id,i.track_key,min(o.approved_at) AS first_save
FROM collection_operations o JOIN collection_operation_items i ON i.operation_id=o.id
WHERE o.action='save' AND o.approved_at IS NOT NULL AND i.after_revision IS NOT NULL
GROUP BY o.owner_id,o.library_id,i.track_key;
CREATE UNIQUE INDEX collection_upgrade_saves_song
    ON collection_upgrade_saves(owner_id,library_id,track_key);
UPDATE collection_history AS h SET activity=1
WHERE h.added_at>0
AND NOT EXISTS (
    SELECT 1 FROM collection_upgrade_saves v
    WHERE v.owner_id=h.owner_id AND v.library_id=h.library_id AND v.track_key=h.track_key
    AND v.first_save<=h.added_at)
AND NOT EXISTS (
    SELECT 1 FROM collection_original_setups s
    JOIN collection_original_setup_accounts a ON a.setup_id=s.id AND a.owner_id=h.owner_id
    WHERE s.state='applied' AND s.applied_at=h.added_at);
DROP TABLE collection_upgrade_saves;
CREATE INDEX collection_history_activity ON collection_history(owner_id,library_id,track_key)
    WHERE activity=1;

-- The built-in membership profiles of Original imported library and Shijhon library,
-- beside All's ("default-<hash>"): "original-<hash>" and "shijhon-<hash>".
-- Only All's stays a live base of the account's imports (``base_original``).
INSERT OR IGNORE INTO collection_profiles(id,owner_id,kind,name,base_original,created)
SELECT 'original-' || substr(id,9),owner_id,'builtin','original-' || substr(id,9),0,created
FROM collection_profiles WHERE kind='builtin' AND id GLOB 'default-*';
INSERT OR IGNORE INTO collection_profiles(id,owner_id,kind,name,base_original,created)
SELECT 'shijhon-' || substr(id,9),owner_id,'builtin','shijhon-' || substr(id,9),0,created
FROM collection_profiles WHERE kind='builtin' AND id GLOB 'default-*';

-- Shijhon library and All shared one profile of hand choices. The catalog songs enabled
-- by hand are Shijhon library's own additions now (and so in All).
UPDATE collection_membership SET profile_id='shijhon-' || substr(profile_id,9)
WHERE saved=1 AND profile_id GLOB 'default-*'
AND EXISTS (SELECT 1 FROM collection_profiles p WHERE p.owner_id=collection_membership.owner_id
    AND p.id=collection_membership.profile_id AND p.kind='builtin')
AND EXISTS (SELECT 1 FROM collection_tracks t
    WHERE t.library_id=collection_membership.library_id
    AND t.track_key=collection_membership.track_key AND t.original=0);
-- A removal stays in the library its change was made in (the change's ``selection``); a
-- removal no change tells about hid the song from both, and stays in both. Copies keep the
-- revision and change, so Undo finds them (``Collection._undo_batch``).
INSERT INTO collection_membership
    (owner_id,profile_id,library_id,track_key,saved,revision,last_operation)
SELECT m.owner_id,'shijhon-' || substr(m.profile_id,9),m.library_id,m.track_key,0,m.revision,
    m.last_operation
FROM collection_membership m
JOIN collection_profiles p ON p.owner_id=m.owner_id AND p.id=m.profile_id AND p.kind='builtin'
LEFT JOIN collection_operations o ON o.id=m.last_operation AND o.owner_id=m.owner_id
WHERE m.saved=0 AND m.profile_id GLOB 'default-*' AND coalesce(o.selection,'')<>'all';
DELETE FROM collection_membership
WHERE saved=0 AND profile_id GLOB 'default-*'
AND EXISTS (SELECT 1 FROM collection_profiles p WHERE p.owner_id=collection_membership.owner_id
    AND p.id=collection_membership.profile_id AND p.kind='builtin')
AND EXISTS (SELECT 1 FROM collection_operations o WHERE o.id=collection_membership.last_operation
    AND o.owner_id=collection_membership.owner_id AND o.selection='shijhon');
-- All kept the account's own imports whatever was chosen; a removal of one in All recorded
-- before had no effect and must not start to have one.
DELETE FROM collection_membership
WHERE saved=0 AND profile_id GLOB 'default-*'
AND EXISTS (SELECT 1 FROM collection_profiles p WHERE p.owner_id=collection_membership.owner_id
    AND p.id=collection_membership.profile_id AND p.kind='builtin')
AND EXISTS (SELECT 1 FROM collection_tracks t
    JOIN collection_library_policy lp ON lp.library_id=t.library_id
    LEFT JOIN collection_originals o ON o.owner_id=collection_membership.owner_id
    AND o.library_id=t.library_id AND o.track_key=t.track_key
    WHERE t.library_id=collection_membership.library_id
    AND t.track_key=collection_membership.track_key
    AND ((t.original=1 AND (lp.mode='legacy_shared' OR o.owner_id IS NOT NULL))
        OR coalesce(o.provenance_id,'') GLOB 'link:*'));
-- The changes made in Shijhon library belong to it: its Recent changes, and their reviews,
-- pending work and Undo act there.
UPDATE collection_operations SET profile_id='shijhon-' || substr(profile_id,9)
WHERE selection='shijhon' AND profile_id GLOB 'default-*';

-- Custom libraries change only by hand: one that grew by the account's imports (a live
-- base) holds the imports it had now, as its own choices.
INSERT INTO collection_membership
    (owner_id,profile_id,library_id,track_key,saved,revision,last_operation)
SELECT p.owner_id,p.id,t.library_id,t.track_key,1,0,''
FROM collection_profiles p
JOIN collection_tracks t
JOIN collection_library_policy lp ON lp.library_id=t.library_id
LEFT JOIN collection_originals o ON o.owner_id=p.owner_id AND o.library_id=t.library_id
    AND o.track_key=t.track_key
WHERE p.kind='custom' AND p.base_original=1
AND ((t.original=1 AND (lp.mode='legacy_shared' OR o.owner_id IS NOT NULL))
    OR coalesce(o.provenance_id,'') GLOB 'link:*')
ON CONFLICT(owner_id,profile_id,library_id,track_key) DO UPDATE SET saved=1
WHERE collection_membership.saved IS NULL;
UPDATE collection_profiles SET base_original=0 WHERE kind='custom' AND base_original=1;
-- A choice undone (NULL) left the song to that base: its own import, now a choice above;
-- any other song was out, and stays out.
UPDATE collection_membership SET saved=0
WHERE saved IS NULL AND EXISTS (SELECT 1 FROM collection_profiles p
    WHERE p.owner_id=collection_membership.owner_id AND p.id=collection_membership.profile_id
    AND p.kind='custom');
-- A song added by hand now brings its whole album: an existing custom library keeps what it
-- holds, with the songs of its albums it left out as removals.
INSERT INTO collection_membership
    (owner_id,profile_id,library_id,track_key,saved,revision,last_operation)
SELECT DISTINCT c.owner_id,c.profile_id,t.library_id,t.track_key,0,0,''
FROM collection_membership c
JOIN collection_profiles p ON p.owner_id=c.owner_id AND p.id=c.profile_id AND p.kind='custom'
JOIN collection_tracks r ON r.library_id=c.library_id AND r.track_key=c.track_key
JOIN collection_tracks t ON t.library_id=r.library_id AND t.album_id=r.album_id
WHERE c.saved=1
AND NOT EXISTS (SELECT 1 FROM collection_membership x WHERE x.owner_id=c.owner_id
    AND x.profile_id=c.profile_id AND x.library_id=t.library_id AND x.track_key=t.track_key)
ON CONFLICT(owner_id,profile_id,library_id,track_key) DO NOTHING;

-- The account's accepted actions not yet recorded in its history (``views/collection_requests.py``,
-- ``CollectionRequests.drain``): an app's or the native website's action too large to
-- confirm with its request, a Downloads-page preparation, favorites read from Navidrome.
-- ``param`` names what ``item`` is, as the action's parameter: a song (``songId``), an
-- album (``albumId``) or either (``id``).
CREATE TABLE collection_pending (
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    param TEXT NOT NULL CHECK(param IN ('id','albumId','songId')),
    item TEXT NOT NULL,
    queued_at REAL NOT NULL,
    -- Failed recordings of it so far; not tried again before ``next_at`` (a backoff of up
    -- to an hour; a start of Shijhon or the same action again makes it due at once).
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_id,library_id,param,item)
);
CREATE INDEX collection_pending_due ON collection_pending(library_id,next_at,queued_at);
-- The favorites read before: the account's activity too, once.
INSERT OR IGNORE INTO collection_pending(owner_id,library_id,param,item,queued_at)
SELECT owner_id,library_id,CASE kind WHEN 'album' THEN 'albumId' ELSE 'songId' END,item_id,
    (julianday('now')-2440587.5)*86400.0
FROM collection_favorites;

-- Carried over once: the catalog albums in the library at this upgrade that no account's
-- record explains (much of what Shijhon added before recorded nothing per account). Not
-- completions of owned albums: those belong with the owned album. They are in no account's
-- library until ``shijhon carry-over --account <name>`` gives them to one account's Shijhon
-- library (``collection/carryover.py``).
CREATE TABLE collection_carryover (
    release_ref TEXT PRIMARY KEY
);
INSERT INTO collection_carryover(release_ref)
SELECT r.ref FROM releases r WHERE r.owned_album_id IS NULL AND r.removing_at IS NULL
EXCEPT SELECT t.release_key FROM collection_history h JOIN collection_tracks t
    ON t.library_id=h.library_id AND t.track_key=h.track_key
EXCEPT SELECT t.release_key FROM collection_originals o JOIN collection_tracks t
    ON t.library_id=o.library_id AND t.track_key=o.track_key
EXCEPT SELECT t.release_key FROM collection_membership m JOIN collection_tracks t
    ON t.library_id=m.library_id AND t.track_key=m.track_key WHERE m.saved=1
EXCEPT SELECT l.release_ref FROM collection_favorites f JOIN track_links l
    ON l.song_id=f.item_id WHERE f.kind='song'
EXCEPT SELECT l.release_ref FROM collection_imported i JOIN track_links l ON l.song_id=i.song_id
EXCEPT SELECT r.ref FROM collection_favorites f JOIN releases r ON r.album_id=f.item_id
    WHERE f.kind='album';
-- The one account they were given to (none until the command names one): its immutable
-- Navidrome user ID, its user name then, and when.
CREATE TABLE collection_carryover_owner (
    slot INTEGER PRIMARY KEY CHECK(slot=1),
    owner_id TEXT NOT NULL CHECK(owner_id<>''),
    username TEXT NOT NULL,
    given_at REAL NOT NULL
);
