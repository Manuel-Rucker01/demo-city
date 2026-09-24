/**
 * NDJSON (newline-delimited JSON) parsing. Handles a truncated final line (no trailing
 * newline, or a run still being written to disk) by silently dropping it rather than throwing.
 */

/** Parse a complete NDJSON string into records, skipping blank lines and a truncated tail line. */
export function parseNdjsonText<T>(text: string): T[] {
  const lines = text.split("\n");
  const out: T[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!.trim();
    if (!line) continue;
    const isLast = i === lines.length - 1;
    try {
      out.push(JSON.parse(line) as T);
    } catch (err) {
      if (isLast) {
        // Truncated last line (no trailing newline written yet, or a partial write) — drop it.
        continue;
      }
      throw new SyntaxError(`Invalid NDJSON on line ${i + 1}: ${(err as Error).message}`);
    }
  }
  return out;
}

/**
 * Stream-parse NDJSON from a fetch Response, calling `onRecord` for each complete line as it
 * arrives. Buffers partial lines across chunk boundaries; a truncated final line is dropped.
 */
export async function streamNdjson<T>(
  response: Response,
  onRecord: (rec: T, index: number) => void,
): Promise<number> {
  if (!response.body) {
    const text = await response.text();
    const records = parseNdjsonText<T>(text);
    records.forEach((r, i) => onRecord(r, i));
    return records.length;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let count = 0;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newlineIdx: number;
    // eslint-disable-next-line no-cond-assign
    while ((newlineIdx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newlineIdx).trim();
      buffer = buffer.slice(newlineIdx + 1);
      if (!line) continue;
      onRecord(JSON.parse(line) as T, count++);
    }
  }
  // Flush whatever's left in the buffer, but only if it parses — a truncated last line
  // (run still being written, or no trailing newline) is silently dropped.
  const tail = buffer.trim();
  if (tail) {
    try {
      onRecord(JSON.parse(tail) as T, count++);
    } catch {
      // truncated tail — drop it
    }
  }
  return count;
}
