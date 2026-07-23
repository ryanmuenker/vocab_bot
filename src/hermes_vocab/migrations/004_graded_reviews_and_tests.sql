ALTER TABLE review_events
ADD COLUMN grade TEXT
    CHECK (grade IS NULL OR grade IN ('correct', 'partial', 'incorrect'));

ALTER TABLE review_events
ADD COLUMN evaluation_feedback TEXT
    CHECK (
        (grade IS NULL AND evaluation_feedback IS NULL)
        OR (
            grade IS NOT NULL
            AND evaluation_feedback IS NOT NULL
            AND length(trim(evaluation_feedback)) > 0
        )
    );

CREATE TABLE test_sessions (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status = 'active' AND completed_at IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX one_active_test_session_idx
    ON test_sessions((1))
    WHERE status = 'active';

CREATE TABLE test_questions (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
        REFERENCES test_sessions(id) ON DELETE CASCADE,
    entry_id INTEGER NOT NULL REFERENCES vocabulary_entries(id),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 5),
    answer_text TEXT,
    grade TEXT CHECK (
        grade IS NULL OR grade IN ('correct', 'partial', 'incorrect')
    ),
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

CREATE INDEX test_questions_session_position_idx
    ON test_questions(session_id, position);

PRAGMA user_version = 4;
