import { describe, expect, it } from "vitest";
import { parseNdjsonText, streamNdjson } from "./ndjson";

interface Row {
  a: number;
  b: string;
}

describe("parseNdjsonText", () => {
  it("parses complete lines", () => {
    const text = '{"a":1,"b":"x"}\n{"a":2,"b":"y"}\n';
    expect(parseNdjsonText<Row>(text)).toEqual([
      { a: 1, b: "x" },
      { a: 2, b: "y" },
    ]);
  });

  it("skips blank lines", () => {
    const text = '{"a":1,"b":"x"}\n\n\n{"a":2,"b":"y"}\n';
    expect(parseNdjsonText<Row>(text)).toHaveLength(2);
  });

  it("drops a truncated last line instead of throwing", () => {
    const text = '{"a":1,"b":"x"}\n{"a":2,"b":"tru';
    expect(parseNdjsonText<Row>(text)).toEqual([{ a: 1, b: "x" }]);
  });

  it("drops a truncated last line with no trailing newline at all", () => {
    const text = '{"a":1,"b":"x"}';
    expect(parseNdjsonText<Row>(text)).toEqual([{ a: 1, b: "x" }]);
  });

  it("throws on invalid JSON that is NOT the last line", () => {
    const text = "{not json}\n" + '{"a":2,"b":"y"}\n';
    expect(() => parseNdjsonText<Row>(text)).toThrow(SyntaxError);
  });

  it("returns empty array for empty input", () => {
    expect(parseNdjsonText<Row>("")).toEqual([]);
  });
});

function makeResponse(body: string, chunked = false): Response {
  if (!chunked) {
    return new Response(body);
  }
  const encoder = new TextEncoder();
  // Split into small chunks, including mid-line, to exercise buffer handling across reads.
  const chunkSize = 7;
  const bytes = encoder.encode(body);
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) {
        controller.enqueue(bytes.slice(i, i + chunkSize));
      }
      controller.close();
    },
  });
  return new Response(stream);
}

describe("streamNdjson", () => {
  it("streams complete lines from a chunked response", async () => {
    const text = '{"a":1,"b":"x"}\n{"a":2,"b":"y"}\n{"a":3,"b":"z"}\n';
    const res = makeResponse(text, true);
    const rows: Row[] = [];
    const count = await streamNdjson<Row>(res, (r) => rows.push(r));
    expect(count).toBe(3);
    expect(rows).toEqual([
      { a: 1, b: "x" },
      { a: 2, b: "y" },
      { a: 3, b: "z" },
    ]);
  });

  it("drops a truncated final line (no trailing newline)", async () => {
    const text = '{"a":1,"b":"x"}\n{"a":2,"b":"trunc';
    const res = makeResponse(text, true);
    const rows: Row[] = [];
    await streamNdjson<Row>(res, (r) => rows.push(r));
    expect(rows).toEqual([{ a: 1, b: "x" }]);
  });

  it("matches parseNdjsonText on the same input", async () => {
    const text = '{"a":1,"b":"x"}\n{"a":2,"b":"y"}\n{"a":3,"b":"z"}\n';
    const viaStream: Row[] = [];
    await streamNdjson<Row>(makeResponse(text, true), (r) => viaStream.push(r));
    expect(viaStream).toEqual(parseNdjsonText<Row>(text));
  });
});
