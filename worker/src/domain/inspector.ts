export const MEMORIZED_STABILITY_DAYS = 30;

export type InspectorStatus = "unseen" | "learning" | "memorized";

export interface InspectorSense {
  readonly id: number;
  readonly definition: string;
  readonly partOfSpeech: string;
  readonly exampleSentence: string;
  readonly sourceContext: string | null;
}

export interface InspectorCard {
  readonly id: number;
  readonly senseId: number | null;
  readonly direction: "forward" | "reverse";
  readonly state: "new" | "review" | "relearning";
  readonly stability: number | null;
  readonly effectiveDueAt: string;
  readonly lastReviewAt: string | null;
  readonly repetitions: number;
  readonly lapses: number;
}

export interface InspectorAttempt {
  readonly id: number;
  readonly cardId: number;
  readonly direction: "forward" | "reverse";
  readonly source: "review" | "test_forward" | "test_reverse" | "migration";
  readonly rating: "again" | "hard" | "good" | "easy";
  readonly evaluatorGrade: "correct" | "partial" | "incorrect" | null;
  readonly reviewedAt: string;
}

export interface InspectorEntry {
  readonly id: number;
  readonly displayText: string;
  readonly normalizedText: string;
  readonly dateAdded: string;
  readonly lastReviewed: string | null;
  readonly status: InspectorStatus;
  readonly due: boolean;
  readonly weakestStability: number | null;
  readonly senses: readonly InspectorSense[];
  readonly cards: readonly InspectorCard[];
  readonly recentAttempts: readonly InspectorAttempt[];
}

export interface InspectorData {
  readonly generatedAt: string;
  readonly memorizedStabilityDays: number;
  readonly summary: {
    readonly total: number;
    readonly unseen: number;
    readonly learning: number;
    readonly memorized: number;
    readonly due: number;
  };
  readonly entries: readonly InspectorEntry[];
}

type SqlRow = Record<string, SqlStorageValue>;

interface EntryRow extends SqlRow {
  id: number;
  display_text: string;
  normalized_text: string;
  date_added: string;
  last_reviewed: string | null;
}

interface SenseRow extends SqlRow {
  id: number;
  entry_id: number;
  definition: string;
  part_of_speech: string;
  example_sentence: string;
  source_context: string | null;
}

interface CardRow extends SqlRow {
  id: number;
  entry_id: number;
  sense_id: number | null;
  direction: "forward" | "reverse";
  state: "new" | "review" | "relearning";
  stability: number | null;
  effective_due_at: string;
  last_review_at: string | null;
  repetitions: number;
  lapses: number;
}

interface AttemptRow extends SqlRow {
  id: number;
  entry_id: number;
  card_id: number;
  direction: "forward" | "reverse";
  source: "review" | "test_forward" | "test_reverse" | "migration";
  rating: "again" | "hard" | "good" | "easy";
  evaluator_grade: "correct" | "partial" | "incorrect" | null;
  reviewed_at: string;
}

function grouped<T extends { readonly entry_id: number }>(rows: Iterable<T>): Map<number, T[]> {
  const byEntry = new Map<number, T[]>();
  for (const row of rows) {
    const values = byEntry.get(row.entry_id);
    if (values === undefined) byEntry.set(row.entry_id, [row]);
    else values.push(row);
  }
  return byEntry;
}

export function readInspectorData(
  storage: DurableObjectStorage,
  nowUtc: string,
): InspectorData {
  const entryRows = Array.from(storage.sql.exec<EntryRow>(
    `SELECT id, display_text, normalized_text, date_added, last_reviewed
       FROM vocabulary_entries
      ORDER BY id`,
  ));
  const senseRows = grouped(storage.sql.exec<SenseRow>(
    `SELECT id, entry_id, definition, part_of_speech, example_sentence, source_context
       FROM vocabulary_senses
      ORDER BY entry_id, id`,
  ));
  const cardRows = grouped(storage.sql.exec<CardRow>(
    `SELECT id, entry_id, sense_id, direction, state, stability, effective_due_at,
            last_review_at, repetitions, lapses
       FROM vocabulary_cards
      ORDER BY entry_id, id`,
  ));
  const attemptRows = grouped(storage.sql.exec<AttemptRow>(
    `SELECT id, entry_id, card_id, direction, source, rating, evaluator_grade, reviewed_at
       FROM (
         SELECT a.id, c.entry_id, a.card_id, c.direction, a.source, a.rating,
                a.evaluator_grade, a.reviewed_at,
                ROW_NUMBER() OVER (
                  PARTITION BY c.entry_id
                  ORDER BY a.reviewed_at DESC, a.id DESC
                ) AS entry_rank
           FROM review_attempts a
           JOIN vocabulary_cards c ON c.id = a.card_id
       )
      WHERE entry_rank <= 3
      ORDER BY entry_id, reviewed_at DESC, id DESC`,
  ));

  const nowTimestamp = Date.parse(nowUtc);
  const summary = { total: entryRows.length, unseen: 0, learning: 0, memorized: 0, due: 0 };
  const entries = entryRows.map((entry): InspectorEntry => {
    const rawCards = cardRows.get(entry.id) ?? [];
    const cards = rawCards.map((card): InspectorCard => ({
      id: card.id,
      senseId: card.sense_id,
      direction: card.direction,
      state: card.state,
      stability: card.stability,
      effectiveDueAt: card.effective_due_at,
      lastReviewAt: card.last_review_at,
      repetitions: card.repetitions,
      lapses: card.lapses,
    }));
    const unseen = cards.length === 0 || cards.every((card) => card.repetitions === 0);
    const memorized = !unseen && cards.every(
      (card) => card.state === "review" &&
        card.stability !== null &&
        card.stability >= MEMORIZED_STABILITY_DAYS,
    );
    const status: InspectorStatus = unseen ? "unseen" : memorized ? "memorized" : "learning";
    const due = cards.some((card) => Date.parse(card.effectiveDueAt) <= nowTimestamp);
    let weakestStability: number | null = null;
    for (const card of cards) {
      if (card.stability !== null &&
          (weakestStability === null || card.stability < weakestStability)) {
        weakestStability = card.stability;
      }
    }

    summary[status] += 1;
    if (due) summary.due += 1;

    return {
      id: entry.id,
      displayText: entry.display_text,
      normalizedText: entry.normalized_text,
      dateAdded: entry.date_added,
      lastReviewed: entry.last_reviewed,
      status,
      due,
      weakestStability,
      senses: (senseRows.get(entry.id) ?? []).map((sense): InspectorSense => ({
        id: sense.id,
        definition: sense.definition,
        partOfSpeech: sense.part_of_speech,
        exampleSentence: sense.example_sentence,
        sourceContext: sense.source_context,
      })),
      cards,
      recentAttempts: (attemptRows.get(entry.id) ?? []).map((attempt): InspectorAttempt => ({
        id: attempt.id,
        cardId: attempt.card_id,
        direction: attempt.direction,
        source: attempt.source,
        rating: attempt.rating,
        evaluatorGrade: attempt.evaluator_grade,
        reviewedAt: attempt.reviewed_at,
      })),
    };
  });

  return {
    generatedAt: nowUtc,
    memorizedStabilityDays: MEMORIZED_STABILITY_DAYS,
    summary,
    entries,
  };
}
