import { afterEach, describe, expect, it, vi } from "vitest";

import { VisualCategory, type SenseCard, type VisualIntent } from "../src/domain/models";
import {
  WikimediaAdapter,
  WIKIMEDIA_TIMEOUT_MS,
  canonicalizeWikimediaMetadata,
} from "../src/integrations/wikimedia";

const SENSE: SenseCard = {
  partOfSpeech: "adjective",
  definition: "Relating to the Greek architectural order with plain column capitals.",
  exampleSentence: "The temple has a Doric colonnade.",
};

const INTENT: VisualIntent = {
  senseIndex: 0,
  category: VisualCategory.ARCHITECTURE,
  query: "Doric order columns",
  description: "Plain columns of the Doric architectural order.",
};

const LOOKUP = { displayText: "Doric", sense: SENSE, intent: INTENT } as const;

type Metadata = Record<string, { value: string }>;

interface PageOptions {
  readonly index?: number;
  readonly title?: string;
  readonly photoUrl?: string;
  readonly sourceUrl?: string;
  readonly mime?: string;
  readonly width?: number;
  readonly height?: number;
  readonly metadata?: Partial<Record<string, string | null>>;
}

function page(options: PageOptions = {}): Record<string, unknown> {
  const metadata: Metadata = {
    Artist: { value: "<a href=\"https://example.test/profile\">Jane Smith</a>" },
    Credit: { value: "Own work" },
    ImageDescription: { value: "Doric order columns on a Greek temple." },
    LicenseShortName: { value: "CC BY-SA 4.0" },
    LicenseUrl: { value: "https://creativecommons.org/licenses/by-sa/4.0/" },
    UsageTerms: { value: "Creative Commons Attribution-Share Alike 4.0" },
    Copyrighted: { value: "True" },
    Restrictions: { value: "" },
  };
  for (const [key, value] of Object.entries(options.metadata ?? {})) {
    if (value === null) delete metadata[key];
    else metadata[key] = { value };
  }
  return {
    pageid: options.index ?? 1,
    ns: 6,
    index: options.index ?? 1,
    title: options.title ?? "File:Doric columns.jpg",
    imageinfo: [{
      thumburl: options.photoUrl ??
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a2/Doric_columns.jpg/1280px-Doric_columns.jpg",
      thumbwidth: options.width ?? 1280,
      thumbheight: options.height ?? 853,
      mime: options.mime ?? "image/jpeg",
      descriptionurl: options.sourceUrl ??
        "https://commons.wikimedia.org/wiki/File:Doric_columns.jpg",
      extmetadata: metadata,
    }],
  };
}

function apiResponse(pages: readonly Record<string, unknown>[], status = 200): Response {
  return new Response(JSON.stringify({ batchcomplete: true, query: { pages } }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("WikimediaAdapter", () => {
  it("returns one relevant, reusable raster candidate with complete attribution", async () => {
    const fetchMock = vi.fn().mockResolvedValue(apiResponse([page()]));
    vi.stubGlobal("fetch", fetchMock);

    const candidate = await new WikimediaAdapter().findPhoto(LOOKUP);

    expect(candidate).toEqual({
      photoUrl:
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a2/Doric_columns.jpg/1280px-Doric_columns.jpg",
      caption:
        "Doric — Relating to the Greek architectural order with plain column capitals.\n\n" +
        "Plain columns of the Doric architectural order.\n\n" +
        "Creator: Jane Smith\n" +
        "Credit: Own work\n" +
        "License: CC BY-SA 4.0 — https://creativecommons.org/licenses/by-sa/4.0/\n" +
        "Source: Wikimedia Commons — https://commons.wikimedia.org/wiki/File:Doric_columns.jpg",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("strips Wikimedia tracking parameters from live thumbnail URLs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(apiResponse([page({
      photoUrl:
        "https://upload.wikimedia.org/wikipedia/commons/1/16/DoricParthenon.jpg" +
        "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=thumbnail_unscaled",
    })]));
    vi.stubGlobal("fetch", fetchMock);

    const candidate = await new WikimediaAdapter().findPhoto(LOOKUP);

    expect(candidate?.photoUrl).toBe(
      "https://upload.wikimedia.org/wikipedia/commons/1/16/DoricParthenon.jpg",
    );
  });

  it("keeps semantic input isolated from fixed Commons control parameters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(apiResponse([page()]));
    vi.stubGlobal("fetch", fetchMock);

    await new WikimediaAdapter().findPhoto(LOOKUP);

    const [requestUrl, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    const url = new URL(requestUrl);
    expect(`${url.origin}${url.pathname}`).toBe("https://commons.wikimedia.org/w/api.php");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      action: "query",
      format: "json",
      formatversion: "2",
      generator: "search",
      gsrsearch: "Doric order columns",
      gsrnamespace: "6",
      gsrlimit: "5",
      gsrsort: "relevance",
      prop: "imageinfo",
      iiprop: "url|mime|size|extmetadata",
      iiurlwidth: "1280",
      iiextmetadatalanguage: "en",
      iiextmetadatafilter:
        "Artist|Credit|ImageDescription|LicenseShortName|LicenseUrl|UsageTerms|Copyrighted|Restrictions",
    });
    expect(request).toMatchObject({ method: "GET", redirect: "manual" });
    expect(new Headers(request.headers)).toMatchObject(expect.any(Headers));
    expect(new Headers(request.headers).get("Api-User-Agent")).toContain("HermesVocabularyCompanion/");
    expect(new Headers(request.headers).get("Accept-Encoding")).toBe("gzip");
  });

  it("rejects query syntax before any request can override Commons parameters", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new WikimediaAdapter();

    for (const query of [
      "Doric & gsrlimit=50",
      "Doric # fragment",
      "\"Doric columns\"",
      "Doric\ncolumns",
      "File:Doric columns",
      "Doric OR columns",
      "intitle:Doric",
    ]) {
      expect(await adapter.findPhoto({
        ...LOOKUP,
        intent: { ...INTENT, query },
      })).toBeNull();
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("skips an off-sense top result and selects a later relevant candidate from the same response", async () => {
    const offSense = page({
      index: 1,
      title: "File:Doric dialect inscription.jpg",
      photoUrl: "https://upload.wikimedia.org/wikipedia/commons/a/a1/Doric_dialect.jpg",
      sourceUrl: "https://commons.wikimedia.org/wiki/File:Doric_dialect_inscription.jpg",
      metadata: { ImageDescription: "An inscription written in the ancient Greek dialect." },
    });
    const relevant = page({ index: 2 });
    const fetchMock = vi.fn().mockResolvedValue(apiResponse([offSense, relevant]));
    vi.stubGlobal("fetch", fetchMock);

    const candidate = await new WikimediaAdapter().findPhoto(LOOKUP);

    expect(candidate?.photoUrl).toContain("Doric_columns.jpg");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("rejects redirects rather than following an unapproved destination", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, {
      status: 302,
      headers: { Location: "https://example.test/redirected" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new WikimediaAdapter().findPhoto(LOOKUP)).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect((fetchMock.mock.calls[0]![1] as RequestInit).redirect).toBe("manual");
  });

  it("rejects non-canonical derivative and source URLs", async () => {
    const invalid: readonly PageOptions[] = [
      { photoUrl: "http://upload.wikimedia.org/wikipedia/commons/a/a2/Doric.jpg" },
      { photoUrl: "https://evil.test/Doric.jpg" },
      { photoUrl: "https://user:pass@upload.wikimedia.org/wikipedia/commons/a/a2/Doric.jpg" },
      { photoUrl: "https://upload.wikimedia.org:8443/wikipedia/commons/a/a2/Doric.jpg" },
      { photoUrl: "https://192.0.2.1/Doric.jpg" },
      { sourceUrl: "https://en.wikipedia.org/wiki/File:Doric_columns.jpg" },
      { sourceUrl: "https://commons.wikimedia.org:444/wiki/File:Doric_columns.jpg" },
      { sourceUrl: "https://commons.wikimedia.org/wiki/File:Different.jpg" },
      { sourceUrl: "not a URL" },
    ];

    for (const options of invalid) {
      const fetchMock = vi.fn().mockResolvedValue(apiResponse([page(options)]));
      vi.stubGlobal("fetch", fetchMock);
      expect(await new WikimediaAdapter().findPhoto(LOOKUP)).toBeNull();
      expect(fetchMock).toHaveBeenCalledOnce();
    }
  });

  it("accepts allowlisted Creative Commons and public-domain metadata only", async () => {
    const accepted = [
      page(),
      page({
        metadata: {
          Artist: null,
          Credit: null,
          LicenseShortName: "Public domain",
          LicenseUrl: null,
          UsageTerms: "Public domain",
          Copyrighted: "False",
        },
      }),
    ];
    for (const candidatePage of accepted) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(apiResponse([candidatePage])));
      expect(await new WikimediaAdapter().findPhoto(LOOKUP)).not.toBeNull();
    }

    const rejected: readonly Partial<Record<string, string | null>>[] = [
      { LicenseShortName: "CC BY-NC 4.0", LicenseUrl: "https://creativecommons.org/licenses/by-nc/4.0/" },
      { LicenseShortName: "CC BY-ND 4.0", LicenseUrl: "https://creativecommons.org/licenses/by-nd/4.0/" },
      { LicenseShortName: "All rights reserved", LicenseUrl: null },
      { LicenseShortName: "CC BY 4.0", LicenseUrl: "https://example.test/license" },
      { LicenseShortName: "CC BY 4.0", LicenseUrl: "https://creativecommons.org/licenses/by-sa/4.0/" },
      { Artist: null, Credit: null },
      { Restrictions: "Editorial use only" },
    ];
    for (const metadata of rejected) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(apiResponse([page({ metadata })])));
      expect(await new WikimediaAdapter().findPhoto(LOOKUP)).toBeNull();
    }
  });

  it("canonicalizes metadata before sensitive-content and relevance decisions", async () => {
    const safeFetch = vi.fn().mockResolvedValue(apiResponse([page({
      metadata: {
        Artist:
          "<span><a href=\"https://evil.test/profile\">Jane&#32;Smith</a></span> https://evil.test/leak \u202e\u200b",
        ImageDescription: "<b>Doric</b> order &amp; columns.",
      },
    })]));
    vi.stubGlobal("fetch", safeFetch);
    const safe = await new WikimediaAdapter().findPhoto(LOOKUP);
    expect(safe?.caption).toContain("Creator: Jane Smith");
    expect(safe?.caption).not.toContain("evil.test");
    expect(safe?.caption).not.toContain("\u202e");

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(apiResponse([page({
      metadata: { ImageDescription: "A blood&#x79; medical illustration of the column." },
    })])));
    expect(await new WikimediaAdapter().findPhoto(LOOKUP)).toBeNull();
  });

  it("bounds decoded plain text while discarding markup, attributes, links, and format controls", () => {
    expect(canonicalizeWikimediaMetadata(
      "<b>A&amp;B</b> <a href=\"https://evil.test/path\">Named&#32;creator</a> " +
        "https://evil.test/raw \u0001\u0085\u202e\u200b X &bogus;",
      20,
    )).toBe("A&B Named creator X");
    expect(canonicalizeWikimediaMetadata("&#65;".repeat(100), 12)).toBe("AAAAAAAAAAAA");
    expect(canonicalizeWikimediaMetadata("&amp;lt;b&amp;gt;", 20)).toBe("&lt;b&gt;");
  });

  it("omits on timeout, fetch and API failures, malformed payloads, or no pages without retrying", async () => {
    vi.useFakeTimers();
    const timeoutFetch = vi.fn((_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }));
    vi.stubGlobal("fetch", timeoutFetch);
    const timedLookup = new WikimediaAdapter().findPhoto(LOOKUP);
    await vi.advanceTimersByTimeAsync(WIKIMEDIA_TIMEOUT_MS);
    await expect(timedLookup).resolves.toBeNull();
    expect(timeoutFetch).toHaveBeenCalledOnce();
    vi.useRealTimers();

    const failures: readonly (() => Promise<Response>)[] = [
      () => Promise.reject(new Error("offline")),
      () => Promise.resolve(apiResponse([], 503)),
      () => Promise.resolve(new Response("{", { status: 200 })),
      () => Promise.resolve(new Response(JSON.stringify({ error: { code: "badrequest" } }), { status: 200 })),
      () => Promise.resolve(new Response(JSON.stringify({ batchcomplete: true }), { status: 200 })),
      () => Promise.resolve(apiResponse([])),
    ];
    for (const failure of failures) {
      const fetchMock = vi.fn(failure);
      vi.stubGlobal("fetch", fetchMock);
      expect(await new WikimediaAdapter().findPhoto(LOOKUP)).toBeNull();
      expect(fetchMock).toHaveBeenCalledOnce();
    }
  });

  it("rejects unsupported media and Telegram-incompatible dimensions without prevalidating bytes", async () => {
    for (const options of [
      { mime: "image/gif" },
      { mime: "image/svg+xml" },
      { width: 0 },
      { width: 9000, height: 1001 },
      { width: 1280, height: 20 },
    ] satisfies readonly PageOptions[]) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(apiResponse([page(options)])));
      expect(await new WikimediaAdapter().findPhoto(LOOKUP)).toBeNull();
    }

    const fetchMock = vi.fn().mockResolvedValue(apiResponse([page()]));
    vi.stubGlobal("fetch", fetchMock);
    expect(await new WikimediaAdapter().findPhoto(LOOKUP)).not.toBeNull();
    const params = new URL(fetchMock.mock.calls[0]![0] as string).searchParams;
    expect([...params.keys()].some((key) => /size|bytes|length/iu.test(key) && key !== "iiprop"))
      .toBe(false);
  });
});
