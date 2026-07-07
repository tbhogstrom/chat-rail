import Fastify from "fastify";
import type Softphone from "ringcentral-softphone";
import { config } from "./config.js";
import { superviseCall, type Supervisor } from "./supervisor.js";
import { msSincePipelineHealthy } from "./liveness.js";

// The bridge is "wedged" if it has active sessions it should be transcribing but
// the pipeline hasn't produced audio within this window. A healthy *80 leg
// streams ~50 packets/s, so any live, working call refreshes the signal
// constantly; only a total pipeline failure (the 2026-07-07 outage) goes stale.
const HEALTH_STALE_MS = 180_000;
const WATCHDOG_INTERVAL_MS = 30_000;

export function buildServer(
  softphone: Softphone,
  // Injectable for testing the start/stop lifecycle without a real softphone.
  supervise: typeof superviseCall = superviseCall,
  // When true, run a self-exit watchdog: if the pipeline stays wedged, exit(1)
  // so Fly's `restart = always` policy brings us back with a fresh SIP
  // registration + Deepgram sockets. Off by default so tests don't self-exit.
  opts: { watchdog?: boolean } = {},
) {
  const app = Fastify({ logger: true });
  const active = new Map<string, Supervisor>();
  // Sessions whose supervisor is being started but hasn't landed in `active`
  // yet. Reserved synchronously so two concurrent POSTs can't both slip past
  // the 409 guard and spawn duplicate supervisors for one session.
  const starting = new Set<string>();

  // True when we're supposed to be transcribing but nothing is flowing.
  const isWedged = () =>
    active.size > 0 && msSincePipelineHealthy() > HEALTH_STALE_MS;

  app.addHook("onRequest", async (req, reply) => {
    // /health must be reachable by Fly's health check, which sends no auth
    // header — exempt it (it exposes no sensitive data).
    if (req.url === "/health") return;
    if (req.headers["x-bridge-key"] !== config.bridge.apiKey) {
      return reply.code(401).send({ error: "unauthorized" });
    }
  });

  app.post<{ Body: { sessionId: string; agentExtNumber: string } }>(
    "/sessions",
    async (req, reply) => {
      const { sessionId, agentExtNumber } = req.body;
      if (!sessionId || !agentExtNumber) {
        return reply.code(400).send({ error: "sessionId and agentExtNumber required" });
      }
      if (active.has(sessionId) || starting.has(sessionId)) {
        return reply.code(409).send({ error: "already supervising", sessionId });
      }
      // Claim the slot BEFORE the first await — closes the TOCTOU race where two
      // simultaneous POSTs both passed the guard and started two supervisors.
      starting.add(sessionId);
      try {
        const sup = await supervise(softphone, sessionId, agentExtNumber, () => {
          // Internal stop (disposed/busy) — release the map slot so retries
          // can take a fresh *80 leg instead of 409'ing forever.
          active.delete(sessionId);
        });
        active.set(sessionId, sup);
        return reply.code(202).send({ sessionId, status: "supervising" });
      } catch (err) {
        req.log.error({ err }, "supervision start failed");
        return reply.code(500).send({ error: String(err) });
      } finally {
        starting.delete(sessionId);
      }
    },
  );

  app.delete<{ Params: { id: string } }>("/sessions/:id", async (req, reply) => {
    const sup = active.get(req.params.id);
    if (!sup) return reply.code(404).send({ error: "not found" });
    await sup.stop();
    active.delete(req.params.id);
    return reply.code(204).send();
  });

  app.get("/health", async (_req, reply) => {
    const wedged = isWedged();
    return reply.code(wedged ? 503 : 200).send({
      ok: !wedged,
      active: [...active.keys()],
      msSincePipelineHealthy: msSincePipelineHealthy(),
    });
  });

  if (opts.watchdog) {
    const timer = setInterval(() => {
      if (isWedged()) {
        console.error(
          `[bridge] WEDGED: ${active.size} active session(s) but no audio for ` +
            `${msSincePipelineHealthy()}ms — exiting for a clean restart`,
        );
        process.exit(1);
      }
    }, WATCHDOG_INTERVAL_MS);
    // Don't let the watchdog keep the event loop (or tests) alive.
    timer.unref();
    app.addHook("onClose", async () => clearInterval(timer));
  }

  return app;
}
