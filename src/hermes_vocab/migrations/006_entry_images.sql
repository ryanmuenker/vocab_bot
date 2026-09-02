CREATE TABLE vocabulary_entry_images (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL UNIQUE
        REFERENCES vocabulary_entries(id) ON DELETE CASCADE,
    sense_id INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN (
            'plant', 'animal', 'architecture', 'object', 'material', 'place',
            'garment', 'food', 'vehicle', 'instrument', 'landform', 'visual style'
        )
    ),
    query TEXT NOT NULL CHECK (length(trim(query)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    photo_url TEXT,
    caption TEXT,
    source_url TEXT,
    telegram_file_id TEXT,
    telegram_file_unique_id TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('capture', 'backfill')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (sense_id, entry_id)
        REFERENCES vocabulary_senses(id, entry_id) ON DELETE CASCADE,
    CHECK (
        (photo_url IS NULL AND caption IS NULL AND source_url IS NULL)
        OR
        (photo_url IS NOT NULL AND length(photo_url) > 0
            AND caption IS NOT NULL AND length(trim(caption)) > 0
            AND length(caption) <= 1024
            AND source_url IS NOT NULL AND length(source_url) > 0)
    ),
    CHECK (
        (telegram_file_id IS NULL AND telegram_file_unique_id IS NULL)
        OR
        (telegram_file_id IS NOT NULL AND length(telegram_file_id) > 0
            AND telegram_file_unique_id IS NOT NULL
            AND length(telegram_file_unique_id) > 0
            AND photo_url IS NOT NULL)
    )
);

CREATE TABLE image_backfill_attempts (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL UNIQUE
        REFERENCES vocabulary_entries(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN (
            'no_visual', 'provider_error', 'rate_limited', 'invalid_response',
            'image_unavailable'
        )
    ),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
    last_error TEXT,
    attempted_at TEXT NOT NULL,
    CHECK (
        (status = 'no_visual' AND last_error IS NULL)
        OR (status != 'no_visual' AND last_error IS NOT NULL
            AND length(trim(last_error)) > 0)
    )
);

CREATE INDEX image_backfill_attempts_status_idx
    ON image_backfill_attempts(status, attempted_at, id);

PRAGMA user_version = 6;
