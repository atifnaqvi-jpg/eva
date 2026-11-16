/**
 * Minimal markdown for streamed answers (WIDG-06, §12.2).
 *
 * Written rather than pulled in because the answer text is untrusted output from a
 * language model: a general-purpose markdown library plus an HTML sanitiser is two
 * dependencies and a configuration mistake away from an XSS in a third party's
 * page. Here the output is a *tree of known elements* — no `innerHTML`, so there is
 * no sanitiser to misconfigure. Anything not in the grammar stays literal text.
 *
 * Supported: **bold**, *italic*, `code`, [text](https://url), fenced code blocks,
 * bullet and numbered lists, paragraphs. Deliberately absent: raw HTML, images
 * (an image URL in a chat answer is an exfiltration channel), tables, headings.
 */

export interface Inline {
  kind: "text" | "bold" | "italic" | "code" | "link";
  text: string;
  href?: string;
}

const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/**
 * Only absolute http(s)/mailto links survive. A relative link is dropped because
 * the chat frame has no sensible base for it, and `javascript:`/`data:` are the
 * classic script-execution URLs.
 */
export function safeHref(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  return SAFE_SCHEMES.has(url.protocol) ? url.toString() : null;
}

export function parseInline(source: string): Inline[] {
  const out: Inline[] = [];
  let buffer = "";
  const flush = (): void => {
    if (buffer) {
      out.push({ kind: "text", text: buffer });
      buffer = "";
    }
  };

  let i = 0;
  while (i < source.length) {
    const rest = source.slice(i);

    if (rest.startsWith("**")) {
      const end = source.indexOf("**", i + 2);
      if (end > i + 2) {
        flush();
        out.push({ kind: "bold", text: source.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }

    if (rest.startsWith("`")) {
      const end = source.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ kind: "code", text: source.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }

    if (rest.startsWith("*")) {
      const end = source.indexOf("*", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ kind: "italic", text: source.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }

    if (rest.startsWith("[")) {
      const close = source.indexOf("](", i + 1);
      const end = close === -1 ? -1 : source.indexOf(")", close + 2);
      if (close !== -1 && end !== -1) {
        const label = source.slice(i + 1, close);
        const href = safeHref(source.slice(close + 2, end));
        flush();
        if (href) out.push({ kind: "link", text: label, href });
        else out.push({ kind: "text", text: label });
        i = end + 1;
        continue;
      }
    }

    buffer += source[i];
    i += 1;
  }
  flush();
  return out;
}

export type Block =
  | { kind: "p"; inline: Inline[] }
  | { kind: "code"; text: string }
  | { kind: "ul"; items: Inline[][] }
  | { kind: "ol"; items: Inline[][] };

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.split("\n");
  let paragraph: string[] = [];

  const flushParagraph = (): void => {
    if (paragraph.length) {
      blocks.push({ kind: "p", inline: parseInline(paragraph.join(" ")) });
      paragraph = [];
    }
  };
  let i = 0;

  while (i < lines.length) {
    const line = lines[i] ?? "";

    if (line.startsWith("```")) {
      flushParagraph();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !(lines[i] ?? "").startsWith("```")) {
        body.push(lines[i] ?? "");
        i += 1;
      }
      i += 1;
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph();
      const items: Inline[][] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i] ?? "")) {
        items.push(parseInline((lines[i] ?? "").replace(/^\s*[-*]\s+/, "")));
        i += 1;
      }
      blocks.push({ kind: "ul", items });
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushParagraph();
      const items: Inline[][] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i] ?? "")) {
        items.push(parseInline((lines[i] ?? "").replace(/^\s*\d+[.)]\s+/, "")));
        i += 1;
      }
      blocks.push({ kind: "ol", items });
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    paragraph.push(line);
    i += 1;
    while (i < lines.length) {
      const next = lines[i] ?? "";
      if (!next.trim() || /^\s*[-*]\s+/.test(next) || /^\s*\d+[.)]\s+/.test(next)) break;
      if (next.startsWith("```")) break;
      paragraph.push(next);
      i += 1;
    }
  }

  flushParagraph();
  return blocks;
}
