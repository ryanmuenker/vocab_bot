CREATE TABLE vocabulary_entries (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL,
    normalized_word TEXT NOT NULL UNIQUE,
    definition TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    example_sentence TEXT NOT NULL,
    date_added TEXT NOT NULL,
    last_reviewed TEXT,
    review_status TEXT NOT NULL DEFAULT 'new'
        CHECK (review_status IN ('new', 'reviewed'))
);

CREATE TABLE review_events (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES vocabulary_entries(id),
    review_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'missed')),
    prompted_at TEXT NOT NULL,
    answered_at TEXT,
    answer_text TEXT
);

CREATE INDEX review_events_entry_id_idx ON review_events(entry_id);
CREATE INDEX vocabulary_review_order_idx
    ON vocabulary_entries(last_reviewed, date_added, id);

PRAGMA user_version = 1;
