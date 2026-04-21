import Fastify from "fastify";
import type Softphone from "ringcentral-softphone";
import { config } from "./config.js";
import { superviseCall, type Supervisor } from "./supervisor.js";

export function buildServer(softphone: Softphone) {
  const app = Fastify({ logger: true });
  const active = new Map<string, Supervisor>();

  app.addHook("onRequest", async (req, reply) => {
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
      if (active.has(sessionId)) {
        return reply.code(409).send({ error: "already supervising", sessionId });
      }
      try {
        const sup = await superviseCall(softphone, sessionId, agentExtNumber);
        active.set(sessionId, sup);
        return reply.code(202).send({ sessionId, status: "supervising" });
      } catch (err) {
        req.log.error({ err }, "supervision start failed");
        return reply.code(500).send({ error: String(err) });
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

  app.get("/health", async () => ({ ok: true, active: [...active.keys()] }));

  return app;
}
