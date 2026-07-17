ALTER TABLE vocabulary_words RENAME TO vocabulary_entries;
ALTER TABLE vocabulary_entries RENAME COLUMN word TO display_text;
ALTER TABLE vocabulary_entries RENAME COLUMN normalized_word TO normalized_text;

ALTER TABLE vocabulary_senses RENAME COLUMN word_id TO entry_id;
ALTER TABLE review_events RENAME COLUMN word_id TO entry_id;

DROP INDEX vocabulary_senses_word_order_idx;
DROP INDEX review_events_word_id_idx;
DROP INDEX vocabulary_review_order_idx;

CREATE INDEX vocabulary_senses_entry_order_idx
    ON vocabulary_senses(entry_id, id);
CREATE INDEX review_events_entry_id_idx ON review_events(entry_id);
CREATE INDEX vocabulary_review_order_idx
    ON vocabulary_entries(last_reviewed, date_added, id);

PRAGMA user_version = 3;
