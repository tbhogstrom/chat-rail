import { describe, it, expect } from "vitest";
import { buildServer } from "./server.js";
import { config } from "./config.js";
import type { Supervisor } from "./supervisor.js";

const headers = {
  "x-bridge-key": config.bridge.apiKey,
  "content-type": "application/json",
};
const payload = { sessionId: "s-race", agentExtNumber: "101" };

describe("POST /sessions concurrency", () => {
  it("starts only ONE supervisor when two POSTs for the same session race", async () => {
    // Regression for the double-supervision race: the active-map slot used to be
    // set only AFTER awaiting superviseCall, so two near-simultaneous POSTs both
    // passed the 409 guard and two supervisors ran for one session.
    let starts = 0;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const supervise = async (): Promise<Supervisor> => {
      starts++;
      await gate; // hold both requests inside superviseCall at once
      return { sessionId: payload.sessionId, stop: async () => {} };
    };

    const app = buildServer({} as never, supervise as never);

    const p1 = app.inject({ method: "POST", url: "/sessions", headers, payload });
    const p2 = app.inject({ method: "POST", url: "/sessions", headers, payload });
    // Let both requests reach the handler/guard before releasing.
    await new Promise((r) => setImmediate(r));
    release();
    const [r1, r2] = await Promise.all([p1, p2]);

    expect(starts).toBe(1);
    expect([r1.statusCode, r2.statusCode].sort()).toEqual([202, 409]);

    await app.close();
  });
});

describe("GET /health", () => {
  it("is reachable WITHOUT the bridge key and reports healthy when idle", async () => {
    // Fly's health check sends no auth header; if /health 401'd, every check
    // would fail and Fly would restart-loop the machine. Idle (no active
    // sessions) must report ok:true / 200.
    const app = buildServer({} as never, (async () => ({
      sessionId: "x",
      stop: async () => {},
    })) as never);

    const res = await app.inject({ method: "GET", url: "/health" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ ok: true, active: [] });

    await app.close();
  });
});
