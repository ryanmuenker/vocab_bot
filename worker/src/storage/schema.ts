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

-- review_events, test_sessions and test_questions are audit carriers for
-- history imported from the pre-card era. Nothing schedules from them.
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

CREATE TABLE IF NOT EXISTS vocabulary_cards (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES vocabulary_entries(id) ON DELETE CASCADE,
  sense_id INTEGER,
  direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
  state TEXT NOT NULL CHECK (state IN ('new', 'review', 'relearning')),
  stability REAL,
  difficulty REAL,
  due_at TEXT NOT NULL,
  effective_due_at TEXT NOT NULL,
  last_review_at TEXT,
  repetitions INTEGER NOT NULL DEFAULT 0 CHECK (repetitions >= 0),
  lapses INTEGER NOT NULL DEFAULT 0 CHECK (lapses >= 0 AND lapses <= repetitions),
  scheduler_kind TEXT NOT NULL,
  scheduler_version TEXT NOT NULL,
  parameters_version TEXT NOT NULL,
  parameter_fingerprint TEXT NOT NULL,
  desired_retention REAL NOT NULL CHECK (
    desired_retention > 0 AND desired_retention < 1
  ),
  introduced_local_date TEXT,
  buried_until_local_date TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (sense_id, entry_id)
    REFERENCES vocabulary_senses(id, entry_id) ON DELETE CASCADE,
  CHECK (
    (direction = 'forward' AND sense_id IS NULL)
    OR (direction = 'reverse' AND sense_id IS NOT NULL)
  ),
  CHECK (
    (
      state = 'new'
      AND stability IS NULL
      AND difficulty IS NULL
      AND last_review_at IS NULL
      AND repetitions = 0
      AND lapses = 0
    )
    OR (
      state IN ('review', 'relearning')
      AND stability IS NOT NULL
      AND stability > 0
      AND difficulty IS NOT NULL
      AND difficulty BETWEEN 1 AND 10
      AND last_review_at IS NOT NULL
      AND repetitions >= 1
    )
  )
);

CREATE TABLE IF NOT EXISTS study_sessions (
  id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('review', 'test_forward', 'test_reverse')),
  status TEXT NOT NULL CHECK (
    status IN ('active', 'interrupted', 'completed', 'exited')
  ),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  local_date TEXT NOT NULL,
  legacy_test_session_id INTEGER UNIQUE REFERENCES test_sessions(id),
  CHECK (
    (status IN ('active', 'interrupted') AND completed_at IS NULL)
    OR (status IN ('completed', 'exited') AND completed_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS study_queue (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
  card_id INTEGER NOT NULL REFERENCES vocabulary_cards(id),
  position INTEGER NOT NULL CHECK (position >= 1),
  status TEXT NOT NULL CHECK (
    status IN ('queued', 'current', 'completed', 'skipped')
  ),
  retry_of_queue_item_id INTEGER REFERENCES study_queue(id),
  completed_attempt_id INTEGER REFERENCES review_attempts(id),
  legacy_test_question_id INTEGER UNIQUE REFERENCES test_questions(id),
  introduced_local_date TEXT,
  UNIQUE (session_id, position),
  CHECK (retry_of_queue_item_id IS NULL OR retry_of_queue_item_id != id),
  CHECK (
    (status = 'completed' AND completed_attempt_id IS NOT NULL)
    OR (status != 'completed' AND completed_attempt_id IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS study_prompts (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
  queue_item_id INTEGER NOT NULL REFERENCES study_queue(id),
  prompt_key TEXT NOT NULL UNIQUE,
  prompt_text TEXT NOT NULL CHECK (length(trim(prompt_text)) > 0),
  status TEXT NOT NULL CHECK (
    status IN ('prepared', 'delivered', 'answered', 'completed', 'failed', 'cancelled')
  ),
  prepared_at TEXT NOT NULL,
  delivered_at TEXT,
  answered_at TEXT,
  CHECK (status != 'prepared' OR delivered_at IS NULL),
  CHECK (status NOT IN ('delivered', 'answered', 'completed') OR delivered_at IS NOT NULL),
  CHECK (status NOT IN ('answered', 'completed') OR answered_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS prompt_delivery_attempts (
  id INTEGER PRIMARY KEY,
  prompt_id INTEGER NOT NULL REFERENCES study_prompts(id) ON DELETE CASCADE,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
  status TEXT NOT NULL CHECK (status IN ('unknown', 'failed', 'delivered')),
  attempted_at TEXT NOT NULL,
  receipt_at TEXT,
  outbound_delivery_id TEXT,
  content_fingerprint TEXT,
  error_text TEXT,
  UNIQUE (prompt_id, attempt_number),
  CHECK (
    (status = 'delivered' AND receipt_at IS NOT NULL AND outbound_delivery_id IS NOT NULL)
    OR status != 'delivered'
  )
);

CREATE TABLE IF NOT EXISTS answer_drafts (
  id INTEGER PRIMARY KEY,
  prompt_id INTEGER NOT NULL UNIQUE REFERENCES study_prompts(id) ON DELETE CASCADE,
  submitted_answer TEXT NOT NULL CHECK (length(trim(submitted_answer)) > 0),
  evaluator_grade TEXT NOT NULL CHECK (
    evaluator_grade IN ('correct', 'partial', 'incorrect')
  ),
  evaluation_feedback TEXT NOT NULL CHECK (length(trim(evaluation_feedback)) > 0),
  answered_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_attempts (
  id INTEGER PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES vocabulary_cards(id),
  session_id INTEGER REFERENCES study_sessions(id),
  queue_item_id INTEGER REFERENCES study_queue(id),
  prompt_id INTEGER REFERENCES study_prompts(id),
  answer_draft_id INTEGER REFERENCES answer_drafts(id),
  source TEXT NOT NULL CHECK (
    source IN ('review', 'test_forward', 'test_reverse', 'migration')
  ),
  rating TEXT NOT NULL CHECK (rating IN ('again', 'hard', 'good', 'easy')),
  submitted_answer TEXT,
  evaluator_grade TEXT CHECK (
    evaluator_grade IS NULL OR evaluator_grade IN ('correct', 'partial', 'incorrect')
  ),
  evaluation_feedback TEXT,
  reviewed_at TEXT NOT NULL,
  before_state TEXT NOT NULL CHECK (before_state IN ('new', 'review', 'relearning')),
  before_stability REAL,
  before_difficulty REAL,
  before_due_at TEXT NOT NULL,
  before_effective_due_at TEXT NOT NULL,
  before_last_review_at TEXT,
  before_repetitions INTEGER NOT NULL,
  before_lapses INTEGER NOT NULL,
  after_state TEXT NOT NULL CHECK (after_state IN ('review', 'relearning')),
  after_stability REAL NOT NULL CHECK (after_stability > 0),
  after_difficulty REAL NOT NULL CHECK (after_difficulty BETWEEN 1 AND 10),
  after_raw_due_at TEXT NOT NULL,
  after_effective_due_at TEXT NOT NULL,
  after_last_review_at TEXT NOT NULL,
  after_repetitions INTEGER NOT NULL CHECK (after_repetitions >= 1),
  after_lapses INTEGER NOT NULL CHECK (after_lapses >= 0),
  scheduler_kind TEXT NOT NULL,
  scheduler_version TEXT NOT NULL,
  parameters_version TEXT NOT NULL,
  parameter_fingerprint TEXT NOT NULL,
  desired_retention REAL NOT NULL CHECK (
    desired_retention > 0 AND desired_retention < 1
  ),
  is_same_session_retry INTEGER NOT NULL DEFAULT 0 CHECK (
    is_same_session_retry IN (0, 1)
  ),
  retry_of_attempt_id INTEGER UNIQUE REFERENCES review_attempts(id),
  legacy_source TEXT,
  legacy_id INTEGER,
  created_at TEXT NOT NULL,
  CHECK ((legacy_source IS NULL) = (legacy_id IS NULL)),
  CHECK (after_repetitions = before_repetitions + 1),
  CHECK (after_lapses <= after_repetitions)
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

CREATE TRIGGER IF NOT EXISTS study_prompt_status_monotonic
BEFORE UPDATE OF status ON study_prompts
WHEN (
  CASE OLD.status
    WHEN 'prepared' THEN 1 WHEN 'delivered' THEN 2 WHEN 'answered' THEN 3
    WHEN 'completed' THEN 4 WHEN 'failed' THEN 4 WHEN 'cancelled' THEN 4
  END
) > (
  CASE NEW.status
    WHEN 'prepared' THEN 1 WHEN 'delivered' THEN 2 WHEN 'answered' THEN 3
    WHEN 'completed' THEN 4 WHEN 'failed' THEN 4 WHEN 'cancelled' THEN 4
  END
)
BEGIN
  SELECT RAISE(ABORT, 'study prompt status cannot regress');
END;

CREATE TRIGGER IF NOT EXISTS study_prompt_terminal_status_immutable
BEFORE UPDATE OF status ON study_prompts
WHEN OLD.status IN ('completed', 'failed', 'cancelled') AND NEW.status != OLD.status
BEGIN
  SELECT RAISE(ABORT, 'study prompt terminal status is immutable');
END;

CREATE TRIGGER IF NOT EXISTS study_prompt_content_immutable
BEFORE UPDATE OF prompt_key, prompt_text, queue_item_id ON study_prompts
BEGIN
  SELECT RAISE(ABORT, 'prepared prompt content is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prompt_delivery_attempts_immutable_update
BEFORE UPDATE ON prompt_delivery_attempts
BEGIN
  SELECT RAISE(ABORT, 'delivery attempts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prompt_delivery_attempts_immutable_delete
BEFORE DELETE ON prompt_delivery_attempts
BEGIN
  SELECT RAISE(ABORT, 'delivery attempts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS answer_drafts_immutable_update
BEFORE UPDATE ON answer_drafts
BEGIN
  SELECT RAISE(ABORT, 'answer drafts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS answer_drafts_immutable_delete
BEFORE DELETE ON answer_drafts
BEGIN
  SELECT RAISE(ABORT, 'answer drafts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS review_attempts_immutable_update
BEFORE UPDATE ON review_attempts
BEGIN
  SELECT RAISE(ABORT, 'review attempts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS review_attempts_immutable_delete
BEFORE DELETE ON review_attempts
BEGIN
  SELECT RAISE(ABORT, 'review attempts are immutable');
END;

CREATE INDEX IF NOT EXISTS vocabulary_senses_entry_order_idx
  ON vocabulary_senses(entry_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS vocabulary_senses_id_entry_idx
  ON vocabulary_senses(id, entry_id);

CREATE UNIQUE INDEX IF NOT EXISTS one_forward_card_per_entry_idx
  ON vocabulary_cards(entry_id) WHERE direction = 'forward';
CREATE UNIQUE INDEX IF NOT EXISTS one_reverse_card_per_sense_idx
  ON vocabulary_cards(sense_id) WHERE direction = 'reverse';
CREATE INDEX IF NOT EXISTS vocabulary_cards_due_idx
  ON vocabulary_cards(effective_due_at, id);
CREATE INDEX IF NOT EXISTS vocabulary_cards_entry_idx
  ON vocabulary_cards(entry_id, direction, sense_id);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_study_session_idx
  ON study_sessions((1)) WHERE status IN ('active', 'interrupted');

CREATE UNIQUE INDEX IF NOT EXISTS one_retry_occurrence_idx
  ON study_queue(retry_of_queue_item_id) WHERE retry_of_queue_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_current_queue_item_idx
  ON study_queue(session_id) WHERE status = 'current';
CREATE INDEX IF NOT EXISTS study_queue_order_idx
  ON study_queue(session_id, position);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_study_prompt_idx
  ON study_prompts((1)) WHERE status IN ('prepared', 'delivered', 'answered');
CREATE UNIQUE INDEX IF NOT EXISTS one_prompt_per_queue_occurrence_idx
  ON study_prompts(queue_item_id);

CREATE UNIQUE INDEX IF NOT EXISTS one_final_attempt_per_prompt_idx
  ON review_attempts(prompt_id) WHERE prompt_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_migration_attempt_per_legacy_row_idx
  ON review_attempts(legacy_source, legacy_id) WHERE legacy_source IS NOT NULL;
CREATE INDEX IF NOT EXISTS review_attempts_card_time_idx
  ON review_attempts(card_id, reviewed_at, id);

CREATE INDEX IF NOT EXISTS inbox_events_actionable_idx
  ON inbox_events(status, id);
CREATE INDEX IF NOT EXISTS inbox_events_capture_key_idx
  ON inbox_events(normalized_key, id);
`;

export function initializeSchema(sql: SqlStorage): void {
  Array.from(sql.exec(SCHEMA_SQL));
}
