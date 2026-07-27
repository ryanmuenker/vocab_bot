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

export class TelegramAdapter {
  constructor(private readonly config: TelegramConfig) {}

  async sendText(text: string): Promise<void> {
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
      const payload = (await response.json()) as { readonly ok?: unknown };
      if (payload.ok !== true) throw new Error("Telegram send rejected");
    }
  }
}
