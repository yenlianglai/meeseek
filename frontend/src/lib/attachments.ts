/**
 * Turning dropped/pasted/picked files into inline parts.
 *
 * Everything is base64 in the request body — there is no upload endpoint to
 * garbage-collect, and an attachment the user removes before sending costs
 * nothing. That trade caps how much can ride along, hence the limits below.
 */
import type { Attachment } from "./types";

/** The image types Gemini accepts. A screenshot pasted from the OS is PNG. */
const IMAGES = ["image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"];

/** Gemini reads a PDF natively — pages arrive as both text and images, so a
 *  spec or a cost sheet can be asked about without any extraction step. */
export const PDF = "application/pdf";

const ACCEPTED = [...IMAGES, PDF];

/** What a file input should offer, kept in step with ACCEPTED. */
export const ACCEPT_ATTR = ACCEPTED.join(",");

export const MAX_FILES = 4;
/** Per-image cap. Base64 inflates by ~4/3, so 5 MB of pixels is ~6.7 MB of
 * request body. */
export const MAX_BYTES = 5 * 1024 * 1024;
/** PDFs get more room because a scanned document is legitimately larger than a
 * screenshot, and one of them usually arrives alone. */
export const MAX_PDF_BYTES = 10 * 1024 * 1024;
/** Across one message, whatever the mix. Four 10 MB PDFs would be ~53 MB of
 * base64 — past the inline-data ceiling, where the request fails outright
 * instead of just losing an attachment. */
export const MAX_TOTAL_BYTES = 12 * 1024 * 1024;

function mb(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function isPdf(a: Attachment): boolean {
  return a.mimeType === PDF;
}

/** Decoded size of an attachment, from the base64 it is carried as: the File
 *  object is long gone by the time the budget has to be re-checked. */
export function attachmentBytes(a: Attachment): number {
  const padding = a.data.endsWith("==") ? 2 : a.data.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((a.data.length * 3) / 4) - padding);
}

export function totalBytes(atts: Attachment[]): number {
  return atts.reduce((n, a) => n + attachmentBytes(a), 0);
}

/** Read one file as bare base64 (the `data:…;base64,` prefix stripped). */
function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.onload = () => {
      const url = String(reader.result);
      const comma = url.indexOf(",");
      resolve(comma >= 0 ? url.slice(comma + 1) : url);
    };
    reader.readAsDataURL(file);
  });
}

/**
 * Convert files to attachments, keeping the ones that fit.
 *
 * Rejections come back as human sentences instead of being dropped silently:
 * a pasted screenshot that vanished with no explanation reads as a broken
 * composer. `room` is how many more attachments the message can still hold,
 * `usedBytes` how much of the message's total budget is already spent.
 */
export async function filesToAttachments(
  files: File[],
  room: number,
  usedBytes = 0,
): Promise<{ attachments: Attachment[]; rejected: string[] }> {
  const attachments: Attachment[] = [];
  const rejected: string[] = [];
  let spent = usedBytes;

  for (const file of files) {
    // Some browsers report no type for a dragged PDF; the extension is the only
    // evidence left, and mislabelling it costs nothing the model cannot see.
    const type = file.type || (/\.pdf$/i.test(file.name) ? PDF : "");
    if (!ACCEPTED.includes(type)) {
      rejected.push(`${file.name || "that file"} is ${file.type || "an unknown type"} — attach a PNG, JPEG or WebP image, or a PDF`);
      continue;
    }
    const cap = type === PDF ? MAX_PDF_BYTES : MAX_BYTES;
    if (file.size > cap) {
      rejected.push(`${file.name} is ${mb(file.size)} — the limit is ${mb(cap)} per ${type === PDF ? "PDF" : "image"}`);
      continue;
    }
    if (attachments.length >= room) {
      rejected.push(`${file.name} was not attached — ${MAX_FILES} files per message is the limit`);
      continue;
    }
    if (spent + file.size > MAX_TOTAL_BYTES) {
      rejected.push(`${file.name} was not attached — one message can carry ${mb(MAX_TOTAL_BYTES)} of attachments in total`);
      continue;
    }
    try {
      attachments.push({
        name: file.name || "pasted image",
        mimeType: type,
        data: await readBase64(file),
      });
      spent += file.size;
    } catch (e) {
      rejected.push(String(e instanceof Error ? e.message : e));
    }
  }
  return { attachments, rejected };
}

/** Render an attachment as a `src` a browser can show without a round trip. */
export function attachmentSrc(a: Attachment): string {
  return a.url ?? `data:${a.mimeType};base64,${a.data}`;
}
