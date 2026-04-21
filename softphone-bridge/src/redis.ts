import { Redis } from "@upstash/redis";
import { config } from "./config.js";

const redis = new Redis({ url: config.redis.url, token: config.redis.token });

const CALL_TTL_SECONDS = 3600;

export async function appendTranscript(sessionId: string, chunk: string): Promise<void> {
  const key = `call:${sessionId}:transcript`;
  const existing = (await redis.get<string>(key)) ?? "";
  const next = existing ? `${existing} ${chunk}`.trim() : chunk;
  await redis.set(key, next, { ex: CALL_TTL_SECONDS });
}

export async function clearTranscript(sessionId: string): Promise<void> {
  await redis.del(`call:${sessionId}:transcript`);
}
