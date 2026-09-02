import { VisualCategory } from "./models";
import type { SenseCard, VisualIntent } from "./models";
import { caseFold, trimPythonWhitespace } from "./normalization";

export const MAX_VISUAL_QUERY_LENGTH = 120;
export const MAX_VISUAL_DESCRIPTION_LENGTH = 240;

const ALLOWED_VISUAL_CATEGORIES: Record<string, VisualCategory> = {
  [VisualCategory.PLANT]: VisualCategory.PLANT,
  [VisualCategory.ANIMAL]: VisualCategory.ANIMAL,
  [VisualCategory.ARCHITECTURE]: VisualCategory.ARCHITECTURE,
  [VisualCategory.OBJECT]: VisualCategory.OBJECT,
  [VisualCategory.MATERIAL]: VisualCategory.MATERIAL,
  [VisualCategory.PLACE]: VisualCategory.PLACE,
  [VisualCategory.GARMENT]: VisualCategory.GARMENT,
  [VisualCategory.FOOD]: VisualCategory.FOOD,
  [VisualCategory.VEHICLE]: VisualCategory.VEHICLE,
  [VisualCategory.INSTRUMENT]: VisualCategory.INSTRUMENT,
  [VisualCategory.LANDFORM]: VisualCategory.LANDFORM,
  [VisualCategory.VISUAL_STYLE]: VisualCategory.VISUAL_STYLE,
};

type BlockedClass =
  | "medical/anatomy"
  | "sexual"
  | "gore"
  | "injury"
  | "procedure"
  | "person/social-role"
  | "action"
  | "event"
  | "emotion"
  | "abstract";

const BLOCKED_CLASS_MARKERS: Record<BlockedClass, readonly string[]> = {
  "medical/anatomy": [
    "paraphimosis", "medical", "medicine", "disease", "disorder", "syndrome",
    "anatomy", "anatomical", "pathology", "pathological", "diagnosis", "symptom",
    "infection", "lesion", "genital", "genitals", "penis", "penile", "foreskin",
    "vagina", "vaginal", "vulva", "uterus", "uterine", "testicle", "testicular",
    "rectum", "rectal", "anus",
  ],
  sexual: [
    "sex", "sexual", "sexuality", "sexually", "intercourse", "erotic", "porn",
    "pornography", "pornographic", "nude", "nudity", "naked", "masturbation",
    "orgasm", "adult content", "adult media", "sexually explicit",
  ],
  gore: [
    "gore", "gory", "gruesome", "corpse", "carcass", "dismembered", "severed",
    "blood", "bloody",
  ],
  injury: [
    "injury", "injured", "wound", "wounded", "trauma", "traumatic", "fracture",
    "fractured", "bleeding",
  ],
  procedure: [
    "procedure", "surgery", "surgical", "incision", "excision", "amputation",
  ],
  "person/social-role": [
    "person", "people", "human", "social role", "occupation", "profession", "worker",
    "employee", "leader", "official", "politician", "soldier", "teacher", "doctor", "nurse",
  ],
  action: ["action", "activity", "act of", "process of", "movement of", "doing"],
  event: ["event", "occurrence", "incident", "ceremony", "festival", "battle", "war"],
  emotion: [
    "emotion", "feeling", "mood", "affection", "anger", "sadness", "joy", "fear",
  ],
  abstract: [
    "duplicity", "abstract", "concept", "idea", "principle", "quality of", "state of",
    "deceit", "deceitful", "deceitfulness", "dishonesty", "double dealing",
  ],
};

const BLOCKED_TOKEN_PREFIXES = [
  "medic", "anatom", "patholog", "diagnos", "symptom", "infect", "lesion",
  "genital", "peni", "vagin", "vulv", "uter", "testicul", "rectal",
  "clinic", "hospital", "physician", "surgeon", "surg", "incis", "excis",
  "amput", "cathet", "scalpel", "syring", "inject", "biops", "endoscop",
  "sex", "porn", "erotic", "nud", "masturb", "orgasm",
  "injur", "wound", "trauma", "fractur", "bleed", "blood", "corpse",
] as const;

const CATEGORY_MARKERS: Record<VisualCategory, readonly string[]> = {
  [VisualCategory.PLANT]: [
    "plant", "plants", "flower", "flowers", "flowering", "tree", "trees", "shrub",
    "shrubs", "herb", "herbs", "botanical", "botany", "genus", "species",
    "perennial", "wildflower", "wildflowers", "blossom", "blossoms",
  ],
  [VisualCategory.ANIMAL]: [
    "animal", "animals", "bird", "birds", "mammal", "mammals", "fish", "insect",
    "insects", "reptile", "reptiles", "amphibian", "amphibians",
  ],
  [VisualCategory.ARCHITECTURE]: [
    "architecture", "architectural", "building", "buildings", "column", "columns",
    "colonnade", "arch", "arches", "temple", "temples", "facade", "tower", "towers",
    "bell tower", "church", "cathedral", "dome", "roof", "structure", "structural",
    "monument",
  ],
  [VisualCategory.OBJECT]: [
    "object", "objects", "tool", "tools", "machine", "machines", "device", "devices",
    "vessel", "container", "furniture", "chair", "table",
  ],
  [VisualCategory.MATERIAL]: [
    "material", "materials", "substance", "metal", "wood", "wooden", "fabric", "stone",
    "glass", "ceramic", "leather",
  ],
  [VisualCategory.PLACE]: [
    "place", "location", "landscape", "site", "city", "country", "village",
    "island", "park", "garden",
  ],
  [VisualCategory.GARMENT]: [
    "garment", "garments", "clothing", "clothes", "dress", "coat", "shirt", "trousers",
    "skirt", "hat", "footwear", "shoe", "shoes",
  ],
  [VisualCategory.FOOD]: [
    "food", "dish", "meal", "fruit", "vegetable", "bread", "cake", "cheese", "dessert",
    "cuisine",
  ],
  [VisualCategory.VEHICLE]: [
    "vehicle", "vehicles", "car", "automobile", "truck", "bus", "train", "aircraft",
    "airplane", "boat", "ship", "bicycle", "motorcycle",
  ],
  [VisualCategory.INSTRUMENT]: [
    "instrument", "instruments", "guitar", "piano", "violin", "horn", "flute", "drum",
    "organ", "harp", "saxophone",
  ],
  [VisualCategory.LANDFORM]: [
    "landform", "mountain", "hill", "valley", "canyon", "cliff", "plateau", "plain",
    "dune", "volcano", "glacier", "waterfall",
  ],
  [VisualCategory.VISUAL_STYLE]: [
    "visual", "style", "pattern", "design", "aesthetic", "appearance", "motif", "colour",
    "color", "texture", "ornament", "decorative",
  ],
};

const BLOCKED_MARKERS = Object.values(BLOCKED_CLASS_MARKERS).flat();
const CATEGORY_MARKER_ENTRIES = Object.entries(CATEGORY_MARKERS) as [
  VisualCategory,
  readonly string[],
][];

const AMBIGUITY_MARKERS = [
  "ambiguous", "unrelated sense", "unrelated senses", "different meanings",
  "multiple distinct", "no dominant", "various unrelated",
] as const;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;
const LITERAL_QUERY = /^[\p{L}\p{M}\p{N}][\p{L}\p{M}\p{N} '\u2019-]*$/u;
const QUERY_OPERATORS: Record<string, true> = { and: true, or: true, not: true };

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function normalizeText(value: string): string {
  return trimPythonWhitespace(value.normalize("NFKC")).replace(/\p{White_Space}+/gu, " ");
}

function searchableText(value: string): string {
  return caseFold(value.normalize("NFKC"))
    .replace(/[^\p{L}\p{M}\p{N}]+/gu, " ")
    .trim();
}

function containsMarker(text: string, markers: readonly string[]): boolean {
  const padded = ` ${searchableText(text)} `;
  return markers.some((marker) => padded.includes(` ${marker} `));
}

function containsBlockedPrefix(text: string): boolean {
  const tokens = searchableText(text).split(" ");
  return tokens.some((token) =>
    BLOCKED_TOKEN_PREFIXES.some((prefix) => token.startsWith(prefix))
  );
}

export function isSensitiveVisualText(value: string): boolean {
  return CONTROL_CHARACTERS.test(value) ||
    containsMarker(value, BLOCKED_MARKERS) ||
    containsBlockedPrefix(value);
}

function categoriesIn(text: string): ReadonlySet<VisualCategory> {
  const padded = ` ${searchableText(text)} `;
  const categories = new Set<VisualCategory>();
  for (const [category, markers] of CATEGORY_MARKER_ENTRIES) {
    if (markers.some((marker) => padded.includes(` ${marker} `))) categories.add(category);
  }
  return categories;
}

export function isValidVisualQuery(value: string): boolean {
  if (CONTROL_CHARACTERS.test(value)) return false;
  const normalized = normalizeText(value);
  if (codePointLength(normalized) < 1 ||
      codePointLength(normalized) > MAX_VISUAL_QUERY_LENGTH ||
      !LITERAL_QUERY.test(normalized)) return false;
  const words = searchableText(normalized).split(" ");
  return !words.some((word) => Object.hasOwn(QUERY_OPERATORS, word));
}

export function isValidVisualDescription(value: string): boolean {
  if (CONTROL_CHARACTERS.test(value)) return false;
  const normalized = normalizeText(value);
  return codePointLength(normalized) >= 1 &&
    codePointLength(normalized) <= MAX_VISUAL_DESCRIPTION_LENGTH;
}

/**
 * Validates optional provider metadata without ever promoting an ineligible entry.
 * A null result is intentionally indistinguishable from an omitted descriptor.
 */
export function validateVisualIntent(
  displayText: string,
  senses: readonly SenseCard[],
  candidate: unknown,
): VisualIntent | null {
  const value = object(candidate);
  if (value === null || !exactKeys(value, ["sense_index", "category", "query", "description"])) {
    return null;
  }
  if (typeof value.sense_index !== "number" || !Number.isInteger(value.sense_index) ||
      value.sense_index < 0 || value.sense_index >= senses.length ||
      typeof value.category !== "string" || !Object.hasOwn(ALLOWED_VISUAL_CATEGORIES, value.category) ||
      typeof value.query !== "string" || typeof value.description !== "string") {
    return null;
  }

  const category = ALLOWED_VISUAL_CATEGORIES[value.category]!;
  if (!isValidVisualQuery(value.query) || !isValidVisualDescription(value.description)) return null;
  const query = normalizeText(value.query);
  const description = normalizeText(value.description);

  const policyTexts = [
    displayText,
    ...senses.flatMap((sense) => [sense.partOfSpeech, sense.definition, sense.exampleSentence]),
    query,
    description,
  ];
  if (policyTexts.some((text) =>
    isSensitiveVisualText(text) ||
    containsMarker(text, AMBIGUITY_MARKERS)
  )) {
    return null;
  }

  const referencedSense = senses[value.sense_index]!;
  for (let index = 0; index < senses.length; index += 1) {
    if (index === value.sense_index) continue;
    const competingSense = senses[index]!;
    const competingCategories = categoriesIn(
      `${competingSense.partOfSpeech} ${competingSense.definition} ${competingSense.exampleSentence}`,
    );
    if ([...competingCategories].some((competing) => competing !== category)) return null;
  }

  return {
    senseIndex: value.sense_index,
    category,
    query,
    description,
  };
}

/** Stable persisted representation shared by capture and backfill. */
export function encodeVisualIntent(intent: VisualIntent): string {
  return JSON.stringify(intent);
}

/** Decode persisted transient intent through the same rejection-only policy. */
export function decodeVisualIntent(
  displayText: string,
  senses: readonly SenseCard[],
  serialized: string,
): VisualIntent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized);
  } catch {
    return null;
  }
  const value = object(parsed);
  if (value === null || !exactKeys(value, ["senseIndex", "category", "query", "description"])) {
    return null;
  }
  return validateVisualIntent(displayText, senses, {
    sense_index: value.senseIndex,
    category: value.category,
    query: value.query,
    description: value.description,
  });
}
