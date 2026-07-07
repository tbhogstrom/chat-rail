// Diagnostic reader for the supervision lifecycle events emitted by
// supervisor.ts -> logSupEvent() into the Upstash Redis list `sup:events`.
//
// Usage (from softphone-bridge/):
//   node read-sup-events.mjs                 # all recent events, chronological
//   node read-sup-events.mjs <sessionIdSub>  # only events whose sessionId contains the substring
//   node read-sup-events.mjs --sessions      # summary: one row per session (attempts, last event)
//
// Reads KV_REST_API_URL / KV_REST_API_TOKEN from .env (same creds the bridge uses).
import "dotenv/config";
import { Redis } from "@upstash/redis";

const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});

const raw = await redis.lrange("sup:events", 0, -1); // newest-first
const events = raw
  .map((e) => (typeof e === "string" ? JSON.parse(e) : e))
  .reverse(); // -> chronological

const arg = process.argv[2];

if (arg === "--sessions") {
  const bySession = new Map();
  for (const e of events) {
    const s = bySession.get(e.sessionId) ?? { events: 0, attempts: 0, last: null, first: e.t };
    s.events++;
    if (e.event === "attempt_start") s.attempts = Math.max(s.attempts, e.attempt ?? 0);
    s.last = `${e.event}${e.reason ? `(${e.reason})` : ""}`;
    s.lastT = e.t;
    bySession.set(e.sessionId, s);
  }
  for (const [sid, s] of bySession) {
    console.log(
      `${sid}  attempts=${s.attempts}  events=${s.events}  first=${s.first}  last=${s.lastT} ${s.last}`,
    );
  }
  console.log(`\n${bySession.size} session(s), ${events.length} event(s) total.`);
} else {
  const filtered = arg ? events.filter((e) => e.sessionId.includes(arg)) : events;
  for (const e of filtered) {
    const { t, sessionId, event, ...extra } = e;
    const sid = sessionId.length > 20 ? `${sessionId.slice(0, 20)}…` : sessionId;
    const kv = Object.entries(extra)
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
    console.log(`${t}  ${sid}  ${event.padEnd(20)} ${kv}`);
  }
  console.log(`\n${filtered.length} event(s).`);
}
