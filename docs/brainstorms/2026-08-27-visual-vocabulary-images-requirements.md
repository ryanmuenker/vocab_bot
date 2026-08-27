---
date: 2026-08-27
topic: visual-vocabulary-images
---

# Visual Vocabulary Images

## Summary

Newly saved vocabulary entries with one clear, high-confidence visual referent will keep the existing definition response and then receive one representative Wikimedia Commons image with attribution.

---

## Problem Frame

Some vocabulary is learned more effectively when its form can be seen rather than described only in prose. Terms such as `Doric` and `aster` refer to visually distinctive forms, while the current response provides definitions and examples but no visual anchor.

Automatic image search can also teach the wrong association. Abstract words, ambiguous words with unrelated senses, and specialized medical terms can produce irrelevant, graphic, or misleading results. Definition delivery is already reliable and must not become dependent on image availability.

---

## Actors

- A1. Learner: requests vocabulary definitions in the private Telegram conversation.
- A2. Vocabulary bot: saves the entry, decides whether an image is appropriate, and delivers the response.
- A3. Wikimedia Commons: supplies reusable image candidates and attribution metadata.

---

## Key Flows

- F1. New visual entry
  - **Trigger:** A1 requests a word that is not already saved.
  - **Actors:** A1, A2, A3
  - **Steps:** A2 defines and saves the entry, sends the complete text response, determines that one visual sense is clear and safe, retrieves one representative image, and sends it with attribution.
  - **Outcome:** A1 receives the reliable definition followed by one useful visual anchor.
  - **Covered by:** R1, R2, R5, R6, R7, R8, R9

- F2. Ineligible or unavailable image
  - **Trigger:** A new entry is abstract, ambiguous, sensitive, low-confidence, or has no usable image result.
  - **Actors:** A1, A2, A3
  - **Steps:** A2 defines and saves the entry, sends the complete text response, and omits the image without presenting an error.
  - **Outcome:** Vocabulary capture behaves exactly as it does today, without delay or misleading media.
  - **Covered by:** R3, R4, R6, R10, R11

---

## Requirements

**Eligibility and relevance**

- R1. Automatic images apply only when a vocabulary entry is newly saved; an already-saved response does not trigger another image lookup.
- R2. An entry is eligible only when it has a clear visual referent, including concrete objects, plants, animals, places, materials, architectural forms, and visually distinctive descriptors.
- R3. The bot must omit the image when the entry has several unrelated visual senses and no single sense is clearly dominant.
- R4. The bot must reject medical/anatomy, sexual, gore, injury, and procedure topics before image lookup and must screen Wikimedia text metadata for the same classes. Pixel moderation is not part of this version; rare mislabeled or vandalized-file risk is accepted.
- R5. Image selection must be grounded in the relevant stored sense, not merely the raw spelling of the requested word.
- R6. Low confidence results in no image; coverage must not be increased by accepting a weak or misleading match.

**Delivery and reliability**

- R7. The complete saved-definition message is sent first and retains its current content and delivery guarantees.
- R8. At most one image is sent, as a separate Telegram photo after the text response.
- R9. The image message includes a concise description plus Wikimedia source, creator when available, license, and a link to the source page.
- R10. Image lookup and delivery are optional enrichment: failure, timeout, malformed metadata, or Telegram photo rejection must not change the saved entry, resend the definition, or surface an image-specific error to the learner.
- R11. The bot must omit candidates that cannot provide a usable image, source page, and required attribution metadata.

---

## Acceptance Examples

- AE1. **Covers R2, R5, R7, R8.** Given a new entry `Doric` whose stored senses clearly identify the architectural order, the bot sends the definition first and then one representative image of Doric architecture.
- AE2. **Covers R2, R5, R7, R8.** Given a new entry `aster` whose dominant sense is the flowering plant, the bot sends the definition first and then one representative aster image.
- AE3. **Covers R3, R6.** Given a word with multiple unrelated visual senses and no dominant sense, the bot sends only the definition.
- AE4. **Covers R2, R6.** Given an abstract entry such as `duplicity`, the bot sends only the definition.
- AE5. **Covers R4.** Given a term such as `paraphimosis`, the bot sends only the definition and performs no user-visible image delivery.
- AE6. **Covers R7, R10, R11.** Given an eligible entry whose Wikimedia lookup times out or yields incomplete attribution, the entry remains saved and the learner receives the normal text response with no image error.
- AE7. **Covers R1.** Given an already-saved visual entry requested again, the bot returns the existing saved response without another image.

---

## Success Criteria

- Concrete and visually distinctive new vocabulary receives a useful image often enough to strengthen recall without becoming expected for every word.
- Known abstract, ambiguous, medical/anatomy, sexual, gory, injury, and procedure cases remain text-only; metadata screening is conservative but does not claim pixel-perfect moderation.
- Definition capture remains successful and unchanged whenever image enrichment is skipped or fails.
- Every delivered image is traceable to its Wikimedia source and license.
- Planning can implement the feature without inventing eligibility, delivery order, failure behavior, repeat-request behavior, or safety policy.

---

## Scope Boundaries

- No images for already-saved entries requested again.
- No image galleries, carousels, or multiple candidates.
- No learner-facing candidate selection or approval step.
- No automatic image for ambiguous words merely because one sense is depictable.
- No broad web image search, generated images, Pexels, or Unsplash in this version.
- No image-specific warning when optional enrichment is unavailable.
- No image-content moderation service or guarantee against a mislabeled/vandalized Commons file.

---

## Key Decisions

- Wikimedia Commons is the source: it provides encyclopedic coverage and reusable images with attribution metadata without adding a paid search dependency.
- Eligibility is conservative: a missing image is preferable to a wrong association.
- Text precedes media: the existing vocabulary response remains the authoritative product behavior.
- Images are first-capture enrichment: repeated requests remain lightweight and deterministic.
- Sensitive filtering is conservative and metadata-based: medical/anatomy, sexual, gore, injury, and procedure topics are excluded before lookup; rare Commons pixel/metadata mismatch risk is accepted.

---

## Dependencies / Assumptions

- Wikimedia Commons remains reachable from the production Worker and exposes a candidate image, source page, and licensing metadata suitable for Telegram delivery.
- The definition result contains enough semantic context to distinguish a dominant visual sense from ambiguity or abstraction.
- Telegram accepts the selected Wikimedia image format or a suitable derivative URL.
- Wikimedia Commons has no SafeSearch; this version intentionally relies on topic and metadata rejection rather than a second image-moderation provider.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R2-R6][Needs research] Determine the smallest reliable eligibility and relevance mechanism that can make one conservative decision without adding a second expensive generation step.
- [Affects R5, R9, R11][Needs research] Determine the Wikimedia query and metadata strategy that best favors representative images and complete attribution.
- [Affects R4][Technical] Define how sensitive medical and explicit-image risk is rejected before any photo is sent.
- [Affects R10][Technical] Fit optional photo delivery into the existing delivery lifecycle without weakening text idempotency or retry safety.
