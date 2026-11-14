-- Pictures Shijhon gave library artists Navidrome found none for ([catalog]
-- artist_pictures), through Navidrome's own artist image upload. ``state``:
-- 'set': its picture, uploaded as ``image`` (the name Navidrome gave it, which is the
--   artist's alone), whose content Navidrome identifies as ``image_hash`` once it has
--   read it; until then the artist's ``updatedAt`` right after the upload
--   (``uploaded_at``) tells it from a person's upload of the same name;
-- 'declined': the picture was replaced or removed in Navidrome: the artist is left alone,
--   also after the setting was off;
-- 'unmatched': no catalog artist was found, or giving the picture failed: asked again
--   from ``retry_at``;
-- 'withdrawn': taken back when Navidrome's external services named a picture
--   (``external_url``). A picture set while Navidrome could not use the ``external_url``
--   it named keeps that address, and stays for it.
CREATE TABLE artist_pictures (
    artist_id TEXT PRIMARY KEY,          -- Navidrome artist ID
    name TEXT NOT NULL,                  -- its name then (for the log)
    state TEXT NOT NULL CHECK (state IN ('set', 'declined', 'unmatched', 'withdrawn')),
    image TEXT,
    image_hash TEXT,
    uploaded_at TEXT,
    catalog_ref TEXT,                    -- the catalog artist whose picture it is
    external_url TEXT,
    updated_at REAL NOT NULL,
    retry_at REAL
);
