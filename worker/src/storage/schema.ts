const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS vocabulary_entries (
  id INTEGER PRIMARY KEY,
  display_text TEXT NOT NULL,
  normalized_text TEXT NOT NULL UNIQUE,
  date_added TEXT NOT NULL,
  last_reviewed TEXT,
  review_status TEXT NOT NULL DEFAULT 'new'
    CHECK (review_status IN ('new', 'reviewed'))
);

CREATE TABLE IF NOT EXISTS vocabulary_senses (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES vocabulary_entries(id) ON DELETE CASCADE,
  definition TEXT NOT NULL,
  part_of_speech TEXT NOT NULL,
  example_sentence TEXT NOT NULL,
  source_context TEXT,
  date_added TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_events (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES vocabulary_entries(id),
  review_date TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'missed')),
  prompted_at TEXT NOT NULL,
  answered_at TEXT,
  answer_text TEXT,
  grade TEXT CHECK (grade IS NULL OR grade IN ('correct', 'partial', 'incorrect')),
  evaluation_feedback TEXT CHECK (
    (grade IS NULL AND evaluation_feedback IS NULL)
    OR (
      grade IS NOT NULL
      AND evaluation_feedback IS NOT NULL
      AND length(trim(evaluation_feedback)) > 0
    )
  )
);

CREATE TABLE IF NOT EXISTS test_sessions (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  CHECK (
    (status = 'active' AND completed_at IS NULL)
    OR (status = 'completed' AND completed_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS test_questions (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES test_sessions(id) ON DELETE CASCADE,
  entry_id INTEGER NOT NULL REFERENCES vocabulary_entries(id),
  position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 5),
  answer_text TEXT,
  grade TEXT CHECK (grade IS NULL OR grade IN ('correct', 'partial', 'incorrect')),
  evaluation_feedback TEXT,
  answered_at TEXT,
  UNIQUE (session_id, position),
  UNIQUE (session_id, entry_id),
  CHECK (
    (
      answer_text IS NULL
      AND grade IS NULL
      AND evaluation_feedback IS NULL
      AND answered_at IS NULL
    )
    OR (
      answer_text IS NOT NULL
      AND length(trim(answer_text)) > 0
      AND grade IS NOT NULL
      AND evaluation_feedback IS NOT NULL
      AND length(trim(evaluation_feedback)) > 0
      AND answered_at IS NOT NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS inbox_events (
  id INTEGER PRIMARY KEY,
  dedupe_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('telegram', 'daily_review')),
  status TEXT NOT NULL CHECK (status IN ('pending', 'waiting', 'ready', 'completed', 'failed')),
  payload TEXT,
  prepared_target_id INTEGER,
  normalized_key TEXT,
  coalesced_to_event_id INTEGER REFERENCES inbox_events(id),
  response_text TEXT,
  next_chunk_index INTEGER NOT NULL DEFAULT 0 CHECK (next_chunk_index >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 10),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_error TEXT,
  CHECK (
    (status = 'waiting' AND coalesced_to_event_id IS NOT NULL)
    OR status != 'waiting'
  )
);

CREATE INDEX IF NOT EXISTS inbox_events_actionable_idx
  ON inbox_events(status, id);
CREATE INDEX IF NOT EXISTS inbox_events_capture_key_idx
  ON inbox_events(normalized_key, id);

CREATE INDEX IF NOT EXISTS vocabulary_senses_entry_order_idx
  ON vocabulary_senses(entry_id, id);
CREATE INDEX IF NOT EXISTS review_events_entry_id_idx
  ON review_events(entry_id);
CREATE INDEX IF NOT EXISTS vocabulary_review_order_idx
  ON vocabulary_entries(last_reviewed, date_added, id);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_test_session_idx
  ON test_sessions((1)) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS test_questions_session_position_idx
  ON test_questions(session_id, position);
`;

export function initializeSchema(sql: SqlStorage): void {
  Array.from(sql.exec(SCHEMA_SQL));
}
