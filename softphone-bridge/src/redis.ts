import { Redis } from "@upstash/redis";
import { config } from "./config.js";

const redis = new Redis({ url: config.redis.url, token: config.redis.token });

const CALL_TTL_SECONDS = 3600;

// Sorted set of sessions that have produced a transcript. Member = sessionId,
// score = first-transcript epoch ms. Powers the dashboard's recent-session
// picker; windowed/pruned to the last hour on the read side (Python).
export const RECENT_SESSIONS_KEY = "sessions:recent";

export async function appendTranscript(sessionId: string, chunk: string): Promise<void> {
  const key = `call:${sessionId}:transcript`;
  const existing = (await redis.get<string>(key)) ?? "";
  const next = existing ? `${existing} ${chunk}`.trim() : chunk;
  await redis.set(key, next, { ex: CALL_TTL_SECONDS });
  if (!existing) {
    // First transcript chunk for this session — index it once so the dashboard
    // can list recently transcribed sessions. `nx` keeps the score pinned to the
    // first-transcript time even though appendTranscript runs on every final.
    await redis.zadd(RECENT_SESSIONS_KEY, { nx: true }, { score: Date.now(), member: sessionId });
  }
}

export async function clearTranscript(sessionId: string): Promise<void> {
  await redis.del(`call:${sessionId}:transcript`);
}

// --- Supervision lifecycle instrumentation -------------------------------
// Durable, queryable record of the supervisor state machine (attempt starts,
// RC-disposed vs IVR-bail, per-attempt audio/duration, source status, stop
// reason). Fly only buffers a few minutes of stdout and no drain is
// configured, so long-call failures age out before we can inspect them. This
// list survives so we can diagnose after the fact. Newest-first (LPUSH),
// capped, self-expiring. Read with: redis.lrange("sup:events", 0, N).
const SUP_EVENTS_KEY = "sup:events";
const SUP_EVENTS_MAX = 5000;
const SUP_EVENTS_TTL_SECONDS = 7 * 24 * 3600;

export async function logSupEvent(
  sessionId: string,
  event: string,
  extra: Record<string, unknown> = {},
): Promise<void> {
  try {
    const entry = JSON.stringify({
      t: new Date().toISOString(),
      sessionId,
      event,
      ...extra,
    });
    await redis.lpush(SUP_EVENTS_KEY, entry);
    await redis.ltrim(SUP_EVENTS_KEY, 0, SUP_EVENTS_MAX - 1);
    await redis.expire(SUP_EVENTS_KEY, SUP_EVENTS_TTL_SECONDS);
  } catch (err) {
    // Instrumentation must NEVER break supervision. Swallow and keep going.
    console.error(`[sup:${sessionId}] logSupEvent failed`, err);
  }
}

export interface CallState {
  status?: string;
  direction?: string;
}

export async function getCallState(sessionId: string): Promise<CallState | null> {
  const raw = await redis.get<string | CallState>(`call:${sessionId}:state`);
  if (raw == null) return null;
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw) as CallState;
    } catch {
      return null;
    }
  }
  return raw;
}
