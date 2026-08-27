export function splitTelegramMessage(text: string, limit = 4_096): string[] {
  if (!Number.isSafeInteger(limit) || limit < 1) throw new RangeError("Telegram chunk limit must be positive");
  const chunks: string[] = [];
  let remaining = Array.from(text);
  while (remaining.length > limit) {
    const window = remaining.slice(0, limit).join("");
    let splitAt = window.lastIndexOf("\n\n");
    let delimiterLength = 2;
    if (splitAt <= 0) {
      splitAt = window.lastIndexOf("\n");
      delimiterLength = 1;
    }
    if (splitAt <= 0) {
      chunks.push(remaining.slice(0, limit).join(""));
      remaining = remaining.slice(limit);
      continue;
    }
    const consumed = Array.from(window.slice(0, splitAt + delimiterLength)).length;
    chunks.push(remaining.slice(0, consumed).join(""));
    remaining = remaining.slice(consumed);
  }
  if (remaining.length > 0 || chunks.length === 0) chunks.push(remaining.join(""));
  return chunks;
}

interface TelegramConfig {
  readonly botToken: string;
  readonly chatId: string;
}

export const TELEGRAM_PHOTO_TIMEOUT_MS = 3_000;

function validPhotoSizeReceipt(value: unknown): boolean {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const photo = value as Record<string, unknown>;
  return (
    typeof photo.file_id === "string" &&
    photo.file_id.length > 0 &&
    typeof photo.file_unique_id === "string" &&
    photo.file_unique_id.length > 0 &&
    Number.isSafeInteger(photo.width) &&
    (photo.width as number) > 0 &&
    Number.isSafeInteger(photo.height) &&
    (photo.height as number) > 0 &&
    (
      photo.file_size === undefined ||
      (Number.isSafeInteger(photo.file_size) && (photo.file_size as number) >= 0)
    )
  );
}

export class TelegramAdapter {
  constructor(private readonly config: TelegramConfig) {}

  /** Sends every chunk in order and returns the Telegram message ids created. */
  async sendText(text: string): Promise<number[]> {
    const messageIds: number[] = [];
    for (const chunk of splitTelegramMessage(text)) {
      const response = await fetch(
        `https://api.telegram.org/bot${this.config.botToken}/sendMessage`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: this.config.chatId, text: chunk }),
        },
      );
      if (!response.ok) throw new Error("Telegram send failed");
      const payload = (await response.json()) as {
        readonly ok?: unknown;
        readonly result?: { readonly message_id?: unknown };
      };
      if (payload.ok !== true) throw new Error("Telegram send rejected");
      const messageId = payload.result?.message_id;
      if (!Number.isSafeInteger(messageId)) {
        throw new Error("Telegram response missing message id");
      }
      messageIds.push(messageId as number);
    }
    return messageIds;
  }

  /** Sends one remote-URL photo with a plain caption and returns its message id. */
  async sendPhoto(photoUrl: string, caption: string): Promise<number> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), TELEGRAM_PHOTO_TIMEOUT_MS);
    try {
      const response = await fetch(
        `https://api.telegram.org/bot${this.config.botToken}/sendPhoto`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chat_id: this.config.chatId,
            photo: photoUrl,
            caption,
          }),
          signal: controller.signal,
        },
      );
      if (!response.ok) throw new Error("Telegram photo send failed");
      const payload = (await response.json()) as {
        readonly ok?: unknown;
        readonly result?: {
          readonly message_id?: unknown;
          readonly photo?: unknown;
        };
      };
      if (payload.ok !== true) throw new Error("Telegram photo send rejected");
      const messageId = payload.result?.message_id;
      if (!Number.isSafeInteger(messageId)) {
        throw new Error("Telegram photo response missing message id");
      }
      const photos = payload.result?.photo;
      if (
        !Array.isArray(photos) ||
        photos.length === 0 ||
        !photos.every(validPhotoSizeReceipt)
      ) {
        throw new Error("Telegram response missing photo receipt");
      }
      return messageId as number;
    } finally {
      clearTimeout(timeout);
    }
  }
}
