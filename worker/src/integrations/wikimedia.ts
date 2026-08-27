import type { SenseCard, VisualIntent } from "../domain/models";
import { formatWikimediaCaption } from "../domain/formatting";
import { caseFold } from "../domain/normalization";
import {
  MAX_VISUAL_DESCRIPTION_LENGTH,
  MAX_VISUAL_QUERY_LENGTH,
  isSensitiveVisualText,
} from "../domain/visual-enrichment";

const WIKIMEDIA_ENDPOINT = "https://commons.wikimedia.org/w/api.php";
const WIKIMEDIA_APPLICATION_AGENT =
  "HermesVocabularyCompanion/1.0 (https://vocab.ryanmuenker.com)";
const WIKIMEDIA_RESULT_LIMIT = 5;
const WIKIMEDIA_THUMB_WIDTH = 1280;
export const WIKIMEDIA_TIMEOUT_MS = 3_000;

const MAX_RAW_METADATA_LENGTH = 20_000;
const MAX_TITLE_LENGTH = 240;
const MAX_DESCRIPTION_LENGTH = 500;
const MAX_ATTRIBUTION_FIELD_LENGTH = 240;
const MAX_LICENSE_FIELD_LENGTH = 120;
const MAX_RESTRICTIONS_LENGTH = 240;
const MAX_IMAGE_DIMENSION_SUM = 10_000;
const MAX_IMAGE_ASPECT_RATIO = 20;

const LITERAL_QUERY = /^[\p{L}\p{M}\p{N}][\p{L}\p{M}\p{N} '\u2019-]*$/u;
const QUERY_OPERATORS: Readonly<Record<string, true>> = {
  and: true,
  or: true,
  not: true,
};
const SUPPORTED_MIME_TYPES: Readonly<Record<string, true>> = {
  "image/jpeg": true,
  "image/png": true,
  "image/webp": true,
};
const ALLOWED_WIKIMEDIA_TRACKING_PARAMETERS: Readonly<Record<string, true>> = {
  utm_source: true,
  utm_campaign: true,
  utm_content: true,
};
const STOP_WORDS: Readonly<Record<string, true>> = {
  a: true,
  an: true,
  and: true,
  are: true,
  as: true,
  at: true,
  be: true,
  by: true,
  file: true,
  for: true,
  from: true,
  has: true,
  image: true,
  in: true,
  is: true,
  it: true,
  of: true,
  on: true,
  or: true,
  photo: true,
  photograph: true,
  that: true,
  the: true,
  this: true,
  to: true,
  was: true,
  were: true,
  with: true,
};
const HTML_ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  apos: "'",
  gt: ">",
  lt: "<",
  nbsp: " ",
  quot: "\"",
};
const FORMAT_CONTROLS =
  /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]/gu;
const RAW_URL = /\b(?:https?:\/\/|www\.)[^\s<>{}\[\]"']+/giu;
const WHITESPACE = /\p{White_Space}+/gu;

export interface WikimediaLookupRequest {
  readonly displayText: string;
  readonly sense: SenseCard;
  readonly intent: VisualIntent;
}

export interface WikimediaPhotoCandidate {
  readonly photoUrl: string;
  readonly caption: string;
}

interface CanonicalMetadata {
  readonly value: string;
  readonly truncated: boolean;
}

interface LicensePolicy {
  readonly name: string;
  readonly url: string | null;
  readonly usageTerms: Readonly<Record<string, true>>;
  readonly copyrighted: "true" | "false";
  readonly attributionRequired: boolean;
}

interface AttributionMetadata {
  readonly creator: string | null;
  readonly credit: string | null;
  readonly license: LicensePolicy;
  readonly licenseUrl: string | null;
}

const LICENSE_POLICIES: Readonly<Record<string, LicensePolicy>> = {
  "cc by 2 0": creativeCommonsPolicy("CC BY 2.0", "by", "2.0", [
    "creative commons attribution 2 0",
  ]),
  "cc by 2 5": creativeCommonsPolicy("CC BY 2.5", "by", "2.5", [
    "creative commons attribution 2 5",
  ]),
  "cc by 3 0": creativeCommonsPolicy("CC BY 3.0", "by", "3.0", [
    "creative commons attribution 3 0",
  ]),
  "cc by 4 0": creativeCommonsPolicy("CC BY 4.0", "by", "4.0", [
    "creative commons attribution 4 0",
  ]),
  "cc by sa 2 0": creativeCommonsPolicy("CC BY-SA 2.0", "by-sa", "2.0", [
    "creative commons attribution share alike 2 0",
    "creative commons attribution sharealike 2 0",
  ]),
  "cc by sa 2 5": creativeCommonsPolicy("CC BY-SA 2.5", "by-sa", "2.5", [
    "creative commons attribution share alike 2 5",
    "creative commons attribution sharealike 2 5",
  ]),
  "cc by sa 3 0": creativeCommonsPolicy("CC BY-SA 3.0", "by-sa", "3.0", [
    "creative commons attribution share alike 3 0",
    "creative commons attribution sharealike 3 0",
  ]),
  "cc by sa 4 0": creativeCommonsPolicy("CC BY-SA 4.0", "by-sa", "4.0", [
    "creative commons attribution share alike 4 0",
    "creative commons attribution sharealike 4 0",
  ]),
  "cc0 1 0": {
    name: "CC0 1.0",
    url: "https://creativecommons.org/publicdomain/zero/1.0/",
    usageTerms: { "cc0 1 0 universal public domain dedication": true, "creative commons cc0 waiver": true },
    copyrighted: "false",
    attributionRequired: false,
  },
  "public domain": {
    name: "Public domain",
    url: null,
    usageTerms: { "public domain": true },
    copyrighted: "false",
    attributionRequired: false,
  },
};

function creativeCommonsPolicy(
  name: string,
  path: "by" | "by-sa",
  version: string,
  usageTerms: readonly string[],
): LicensePolicy {
  return {
    name,
    url: `https://creativecommons.org/licenses/${path}/${version}/`,
    usageTerms: Object.fromEntries(usageTerms.map((term) => [term, true])),
    copyrighted: "true",
    attributionRequired: true,
  };
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function findTagEnd(value: string, start: number): number {
  let quote: "\"" | "'" | null = null;
  for (let index = start + 1; index < value.length; index += 1) {
    const character = value[index]!;
    if (quote !== null) {
      if (character === quote) quote = null;
    } else if (character === "\"" || character === "'") {
      quote = character;
    } else if (character === ">") {
      return index;
    }
  }
  return -1;
}

function extractTextNodes(value: string): string {
  let output = "";
  let cursor = 0;
  let suppressedTag: "script" | "style" | null = null;
  while (cursor < value.length) {
    const character = value[cursor]!;
    if (character !== "<") {
      if (suppressedTag === null) output += character;
      cursor += 1;
      continue;
    }

    const tagEnd = findTagEnd(value, cursor);
    if (tagEnd < 0) break;
    const tag = value.slice(cursor + 1, tagEnd).trim();
    const match = /^(\/)?\s*([A-Za-z][A-Za-z0-9]*)/u.exec(tag);
    const tagName = match?.[2]?.toLowerCase();
    const closing = match?.[1] === "/";
    if (tagName === "script" || tagName === "style") {
      if (closing && suppressedTag === tagName) suppressedTag = null;
      else if (!closing && suppressedTag === null) suppressedTag = tagName;
    }
    if (suppressedTag === null) output += " ";
    cursor = tagEnd + 1;
  }
  return output;
}

function sanitizeMetadata(value: string, maxCodePoints: number): CanonicalMetadata {
  const textNodes = extractTextNodes(value.slice(0, MAX_RAW_METADATA_LENGTH));
  const decoded = textNodes.replace(
    /&(?:#([0-9]{1,7})|#x([0-9a-fA-F]{1,6})|([A-Za-z][A-Za-z0-9]{1,31}));/gu,
    (_entity, decimal: string | undefined, hexadecimal: string | undefined,
      named: string | undefined) => {
      if (decimal !== undefined || hexadecimal !== undefined) {
        const codePoint = Number.parseInt(decimal ?? hexadecimal!, decimal === undefined ? 16 : 10);
        return Number.isInteger(codePoint) && codePoint > 0 && codePoint <= 0x10ffff &&
            !(codePoint >= 0xd800 && codePoint <= 0xdfff)
          ? String.fromCodePoint(codePoint)
          : " ";
      }
      return named !== undefined && Object.hasOwn(HTML_ENTITIES, named)
        ? HTML_ENTITIES[named]!
        : " ";
    },
  );
  const canonical = decoded
    .normalize("NFKC")
    .replace(FORMAT_CONTROLS, "")
    .replace(RAW_URL, " ")
    .replace(WHITESPACE, " ")
    .trim();
  const codePoints = Array.from(canonical);
  return {
    value: codePoints.slice(0, maxCodePoints).join(""),
    truncated: value.length > MAX_RAW_METADATA_LENGTH || codePoints.length > maxCodePoints,
  };
}

/** Canonicalizes untrusted Commons extmetadata into bounded display-safe plain text. */
export function canonicalizeWikimediaMetadata(value: string, maxCodePoints: number): string {
  if (!Number.isInteger(maxCodePoints) || maxCodePoints < 0) return "";
  return sanitizeMetadata(value, maxCodePoints).value;
}

function searchableToken(value: string): string {
  let token = caseFold(value.normalize("NFKC"));
  if (token.length > 5 && token.endsWith("ing")) token = token.slice(0, -3);
  else if (token.length > 4 && token.endsWith("ies")) token = `${token.slice(0, -3)}y`;
  else if (token.length > 4 && token.endsWith("s") && !token.endsWith("ss")) {
    token = token.slice(0, -1);
  }
  return token;
}

function distinctiveTokens(value: string): Set<string> {
  const tokens = new Set<string>();
  for (const match of value.normalize("NFKC").matchAll(/[\p{L}\p{M}\p{N}]+/gu)) {
    const token = searchableToken(match[0]);
    if (token.length >= 3 && !Object.hasOwn(STOP_WORDS, token)) tokens.add(token);
  }
  return tokens;
}

function intersectionSize(left: ReadonlySet<string>, right: ReadonlySet<string>): number {
  let matches = 0;
  for (const token of left) {
    if (right.has(token)) matches += 1;
  }
  return matches;
}

function isRelevantCandidate(
  title: string,
  description: string,
  request: WikimediaLookupRequest,
): boolean {
  const entryTokens = distinctiveTokens(request.displayText);
  const primaryTokens = distinctiveTokens(`${request.intent.query} ${request.intent.description}`);
  const senseTokens = distinctiveTokens(
    `${request.sense.partOfSpeech} ${request.sense.definition} ${request.sense.exampleSentence}`,
  );
  for (const token of entryTokens) {
    primaryTokens.delete(token);
    senseTokens.delete(token);
  }
  const candidateTokens = distinctiveTokens(`${title} ${description}`);
  return intersectionSize(primaryTokens, candidateTokens) >= 1 ||
    intersectionSize(senseTokens, candidateTokens) >= 2;
}

function normalizeLicenseText(value: string): string {
  return caseFold(value.normalize("NFKC"))
    .replace(/[^\p{L}\p{M}\p{N}]+/gu, " ")
    .trim();
}

function metadataValue(
  metadata: Record<string, unknown>,
  key: string,
  maxCodePoints: number,
): CanonicalMetadata | null {
  const field = object(metadata[key]);
  return field !== null && typeof field.value === "string"
    ? sanitizeMetadata(field.value, maxCodePoints)
    : null;
}

function rawMetadataValue(
  metadata: Record<string, unknown>,
  key: string,
  maxLength: number,
): string | null | undefined {
  if (!Object.hasOwn(metadata, key)) return undefined;
  const field = object(metadata[key]);
  if (field === null || typeof field.value !== "string" || field.value.length > maxLength) {
    return null;
  }
  return field.value;
}

function validateMappedLicenseUrl(value: string, canonicalUrl: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (
    url.protocol !== "https:" ||
    url.hostname !== "creativecommons.org" ||
    url.username.length > 0 ||
    url.password.length > 0 ||
    url.port.length > 0 ||
    url.search.length > 0 ||
    url.hash.length > 0
  ) return false;
  const normalized = `${url.origin}${url.pathname.replace(/\/*$/u, "/")}`;
  return normalized === canonicalUrl;
}

function parseAttribution(metadata: Record<string, unknown>): AttributionMetadata | null {
  const creatorField = metadataValue(metadata, "Artist", MAX_ATTRIBUTION_FIELD_LENGTH);
  const creditField = metadataValue(metadata, "Credit", MAX_ATTRIBUTION_FIELD_LENGTH);
  const licenseNameField = metadataValue(metadata, "LicenseShortName", MAX_LICENSE_FIELD_LENGTH);
  const licenseUrlField = rawMetadataValue(metadata, "LicenseUrl", 500);
  const usageTermsField = metadataValue(metadata, "UsageTerms", MAX_LICENSE_FIELD_LENGTH);
  const copyrightedField = metadataValue(metadata, "Copyrighted", 10);
  const restrictionsField = metadataValue(metadata, "Restrictions", MAX_RESTRICTIONS_LENGTH);
  if (
    creatorField?.truncated === true || creditField?.truncated === true ||
    licenseNameField === null || licenseNameField.truncated || licenseNameField.value.length === 0 ||
    usageTermsField === null || usageTermsField.truncated || usageTermsField.value.length === 0 ||
    copyrightedField === null || copyrightedField.truncated ||
    restrictionsField === null || restrictionsField.truncated || restrictionsField.value.length > 0
  ) return null;

  const license = LICENSE_POLICIES[normalizeLicenseText(licenseNameField.value)];
  if (
    license === undefined ||
    !Object.hasOwn(license.usageTerms, normalizeLicenseText(usageTermsField.value)) ||
    normalizeLicenseText(copyrightedField.value) !== license.copyrighted
  ) return null;

  if (licenseUrlField === null) return null;
  let licenseUrl: string | null = null;
  if (license.url !== null) {
    if (
      licenseUrlField === undefined ||
      !validateMappedLicenseUrl(licenseUrlField, license.url)
    ) return null;
    licenseUrl = license.url;
  } else if (licenseUrlField !== undefined && licenseUrlField.length > 0) {
    const publicDomainMark = "https://creativecommons.org/publicdomain/mark/1.0/";
    if (!validateMappedLicenseUrl(licenseUrlField, publicDomainMark)) return null;
    licenseUrl = publicDomainMark;
  }

  const creator = creatorField?.value || null;
  let credit = creditField?.value || null;
  if (credit === creator) credit = null;
  if (license.attributionRequired && creator === null && credit === null) return null;
  if ([creator, credit, licenseNameField.value, usageTermsField.value, restrictionsField.value]
    .some((text) => text !== null && isSensitiveVisualText(text))) return null;

  return { creator, credit, license, licenseUrl };
}

function validQuery(request: WikimediaLookupRequest): boolean {
  const { query, description } = request.intent;
  if (
    Array.from(query).length < 1 || Array.from(query).length > MAX_VISUAL_QUERY_LENGTH ||
    Array.from(description).length < 1 ||
    Array.from(description).length > MAX_VISUAL_DESCRIPTION_LENGTH ||
    !LITERAL_QUERY.test(query)
  ) return false;
  const queryTokens = caseFold(query.normalize("NFKC")).match(/[\p{L}\p{M}\p{N}]+/gu) ?? [];
  if (queryTokens.some((token) => Object.hasOwn(QUERY_OPERATORS, token))) return false;
  return [
    request.displayText,
    request.sense.partOfSpeech,
    request.sense.definition,
    request.sense.exampleSentence,
    query,
    description,
  ].every((text) => !isSensitiveVisualText(text));
}

function validateHttpsAuthority(value: string, hostname: string): URL | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  return url.protocol === "https:" && url.hostname === hostname &&
      url.username.length === 0 && url.password.length === 0 && url.port.length === 0
    ? url
    : null;
}

function validatePhotoUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const url = validateHttpsAuthority(value, "upload.wikimedia.org");
  if (url === null || !url.pathname.startsWith("/wikipedia/commons/") || url.hash.length > 0) {
    return null;
  }
  let supportedParameters = true;
  url.searchParams.forEach((_value, key) => {
    if (!Object.hasOwn(ALLOWED_WIKIMEDIA_TRACKING_PARAMETERS, key)) supportedParameters = false;
  });
  if (!supportedParameters) return null;
  url.search = "";
  return url.href;
}

function validateSourceUrl(value: unknown, title: string): string | null {
  if (typeof value !== "string") return null;
  const url = validateHttpsAuthority(value, "commons.wikimedia.org");
  if (url === null || url.search.length > 0 || url.hash.length > 0 ||
      !url.pathname.startsWith("/wiki/")) return null;
  let sourceTitle: string;
  try {
    sourceTitle = decodeURIComponent(url.pathname.slice("/wiki/".length)).replaceAll("_", " ");
  } catch {
    return null;
  }
  return sourceTitle === title ? url.href : null;
}

function validImageDimensions(width: unknown, height: unknown): boolean {
  if (
    typeof width !== "number" || typeof height !== "number" ||
    !Number.isInteger(width) || !Number.isInteger(height) ||
    width <= 0 || height <= 0 || width + height > MAX_IMAGE_DIMENSION_SUM
  ) return false;
  return Math.max(width, height) / Math.min(width, height) <= MAX_IMAGE_ASPECT_RATIO;
}

function parseCandidate(
  page: unknown,
  request: WikimediaLookupRequest,
): WikimediaPhotoCandidate | null {
  const pageObject = object(page);
  if (pageObject === null || pageObject.ns !== 6 || typeof pageObject.title !== "string") return null;
  const canonicalTitle = sanitizeMetadata(pageObject.title, MAX_TITLE_LENGTH);
  if (canonicalTitle.truncated || !canonicalTitle.value.startsWith("File:") ||
      isSensitiveVisualText(canonicalTitle.value)) return null;

  if (!Array.isArray(pageObject.imageinfo) || pageObject.imageinfo.length !== 1) return null;
  const imageInfo = object(pageObject.imageinfo[0]);
  if (
    imageInfo === null || typeof imageInfo.mime !== "string" ||
    !Object.hasOwn(SUPPORTED_MIME_TYPES, imageInfo.mime) ||
    !validImageDimensions(imageInfo.thumbwidth, imageInfo.thumbheight)
  ) return null;
  const photoUrl = validatePhotoUrl(imageInfo.thumburl);
  const sourceUrl = validateSourceUrl(imageInfo.descriptionurl, canonicalTitle.value);
  if (photoUrl === null || sourceUrl === null) return null;

  const metadata = object(imageInfo.extmetadata);
  if (metadata === null) return null;
  const descriptionField = metadataValue(metadata, "ImageDescription", MAX_DESCRIPTION_LENGTH);
  const description = descriptionField?.value ?? "";
  if (
    descriptionField?.truncated === true ||
    isSensitiveVisualText(description) ||
    !isRelevantCandidate(canonicalTitle.value, description, request)
  ) return null;
  const attribution = parseAttribution(metadata);
  if (attribution === null) return null;

  const caption = formatWikimediaCaption({
    entryName: request.displayText,
    senseDescription: request.sense.definition,
    imageDescription: request.intent.description,
    creator: attribution.creator,
    credit: attribution.credit,
    licenseName: attribution.license.name,
    licenseUrl: attribution.licenseUrl,
    sourceUrl,
  });
  return caption === null ? null : { photoUrl, caption };
}

function buildRequestUrl(query: string): string {
  const url = new URL(WIKIMEDIA_ENDPOINT);
  const fixedParameters: Readonly<Record<string, string>> = {
    action: "query",
    format: "json",
    formatversion: "2",
    generator: "search",
    gsrnamespace: "6",
    gsrlimit: String(WIKIMEDIA_RESULT_LIMIT),
    gsrsort: "relevance",
    prop: "imageinfo",
    iiprop: "url|mime|size|extmetadata",
    iiurlwidth: String(WIKIMEDIA_THUMB_WIDTH),
    iiextmetadatalanguage: "en",
    iiextmetadatafilter:
      "Artist|Credit|ImageDescription|LicenseShortName|LicenseUrl|UsageTerms|Copyrighted|Restrictions",
  };
  for (const [key, value] of Object.entries(fixedParameters)) url.searchParams.set(key, value);
  url.searchParams.set("gsrsearch", query);
  return url.href;
}

export class WikimediaAdapter {
  async findPhoto(request: WikimediaLookupRequest): Promise<WikimediaPhotoCandidate | null> {
    if (!validQuery(request)) return null;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), WIKIMEDIA_TIMEOUT_MS);
    try {
      const response = await fetch(buildRequestUrl(request.intent.query), {
        method: "GET",
        headers: {
          Accept: "application/json",
          "Accept-Encoding": "gzip",
          "Api-User-Agent": WIKIMEDIA_APPLICATION_AGENT,
        },
        redirect: "manual",
        signal: controller.signal,
      });
      if (response.status >= 300 && response.status < 400) return null;
      if (!response.ok) return null;

      let body: unknown;
      try {
        body = await response.json();
      } catch {
        return null;
      }
      const payload = object(body);
      if (payload === null || Object.hasOwn(payload, "error")) return null;
      const query = object(payload.query);
      if (
        query === null || !Array.isArray(query.pages) ||
        query.pages.length > WIKIMEDIA_RESULT_LIMIT
      ) return null;
      const pages = query.pages.map((page, position) => ({ page, position }));
      pages.sort((left, right) => {
        const leftPage = object(left.page);
        const rightPage = object(right.page);
        const leftIndex = typeof leftPage?.index === "number" ? leftPage.index : left.position;
        const rightIndex = typeof rightPage?.index === "number" ? rightPage.index : right.position;
        return leftIndex - rightIndex;
      });
      for (const result of pages.slice(0, WIKIMEDIA_RESULT_LIMIT)) {
        const candidate = parseCandidate(result.page, request);
        if (candidate !== null) return candidate;
      }
      return null;
    } catch {
      return null;
    } finally {
      clearTimeout(timeout);
    }
  }
}
