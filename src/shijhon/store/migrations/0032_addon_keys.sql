-- Each add-on's fixed key (delivery.addon.addon_key): what its catalog's items are tagged
-- with and referenced by, and what the routing asks it for its own songs by. Until now it
-- was made from the name each time, so a rename made the add-on another catalog; from now
-- on it is stored, given when the add-on is added, and a rename changes only the name.
-- Existing add-ons get the key their name made, so every stored ID keeps resolving. Names
-- were not unique in storage: of several add-ons with one name, the one that answered for
-- it (the first enabled, in the list's order) keeps that key; the others get keys of
-- their own.
ALTER TABLE sources ADD COLUMN key TEXT NOT NULL DEFAULT '';
UPDATE sources SET key = shijhon_addon_key(name, 1) WHERE id = (
    SELECT same.id FROM sources AS same WHERE same.name = sources.name
    ORDER BY same.enabled DESC, same.position, same.id LIMIT 1
);
UPDATE sources SET key = shijhon_addon_key(name, id + 1) WHERE key = '';
CREATE UNIQUE INDEX sources_key ON sources (key);
