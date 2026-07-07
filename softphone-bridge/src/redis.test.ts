import { describe, it, expect, vi, beforeEach } from "vitest";

// Hoisted so the mock factory (also hoisted) can close over them safely.
const h = vi.hoisted(() => {
  const kv = new Map<string, string>();
  return {
    kv,
    get: vi.fn(async (k: string) => kv.get(k) ?? null),
    set: vi.fn(async (k: string, v: string) => {
      kv.set(k, v);
    }),
    zadd: vi.fn(async () => 1),
  };
});

vi.mock("@upstash/redis", () => ({
  Redis: class {
    get = h.get;
    set = h.set;
    zadd = h.zadd;
    lpush = vi.fn();
    ltrim = vi.fn();
    expire = vi.fn();
  },
}));

const { appendTranscript, RECENT_SESSIONS_KEY } = await import("./redis.js");

describe("appendTranscript indexing", () => {
  beforeEach(() => {
    h.kv.clear();
    h.zadd.mockClear();
    h.get.mockClear();
    h.set.mockClear();
  });

  it("indexes sessions:recent on the FIRST chunk only", async () => {
    await appendTranscript("s-1", "hello");
    expect(h.zadd).toHaveBeenCalledTimes(1);
    expect(h.zadd).toHaveBeenCalledWith(
      RECENT_SESSIONS_KEY,
      { nx: true },
      { score: expect.any(Number), member: "s-1" },
    );

    await appendTranscript("s-1", "world");
    expect(h.zadd).toHaveBeenCalledTimes(1); // second chunk must NOT re-index
  });

  it("stores the joined transcript", async () => {
    await appendTranscript("s-2", "a");
    await appendTranscript("s-2", "b");
    expect(h.kv.get("call:s-2:transcript")).toBe("a b");
  });
});
