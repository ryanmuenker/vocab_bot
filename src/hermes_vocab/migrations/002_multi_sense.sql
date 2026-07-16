BEGIN IMMEDIATE;

ALTER TABLE vocabulary_entries RENAME TO vocabulary_entries_v1;
ALTER TABLE review_events RENAME TO review_events_v1;

CREATE TABLE vocabulary_words (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL,
    normalized_word TEXT NOT NULL UNIQUE,
    date_added TEXT NOT NULL,
    last_reviewed TEXT,
    review_status TEXT NOT NULL DEFAULT 'new'
        CHECK (review_status IN ('new', 'reviewed'))
);

CREATE TABLE vocabulary_senses (
    id INTEGER PRIMARY KEY,
    word_id INTEGER NOT NULL REFERENCES vocabulary_words(id) ON DELETE CASCADE,
    definition TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    example_sentence TEXT NOT NULL,
    source_context TEXT,
    date_added TEXT NOT NULL
);

CREATE TABLE review_events (
    id INTEGER PRIMARY KEY,
    word_id INTEGER NOT NULL REFERENCES vocabulary_words(id),
    review_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'missed')),
    prompted_at TEXT NOT NULL,
    answered_at TEXT,
    answer_text TEXT
);

INSERT INTO vocabulary_words (
    id, word, normalized_word, date_added, last_reviewed, review_status
)
SELECT id, word, normalized_word, date_added, last_reviewed, review_status
FROM vocabulary_entries_v1;

INSERT INTO vocabulary_senses (
    word_id, definition, part_of_speech, example_sentence, source_context, date_added
)
SELECT id, definition, part_of_speech, example_sentence, NULL, date_added
FROM vocabulary_entries_v1;

INSERT INTO review_events (
    id, word_id, review_date, status, prompted_at, answered_at, answer_text
)
SELECT id, entry_id, review_date, status, prompted_at, answered_at, answer_text
FROM review_events_v1;

DROP TABLE review_events_v1;
DROP TABLE vocabulary_entries_v1;

CREATE INDEX vocabulary_senses_word_order_idx
    ON vocabulary_senses(word_id, date_added, id);
CREATE INDEX review_events_word_id_idx ON review_events(word_id);
CREATE INDEX vocabulary_review_order_idx
    ON vocabulary_words(last_reviewed, date_added, id);

PRAGMA user_version = 2;
