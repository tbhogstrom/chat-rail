import { DeepgramClient } from "@deepgram/sdk";
import { config } from "./config.js";

/** Reject if `p` doesn't settle within `ms`. Clears its timer either way. */
function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    p.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}

export interface DeepgramSession {
  sendAudio(payload: Buffer): void;
  close(): Promise<void>;
}

// Bound on establishing the streaming socket. If Deepgram doesn't open within
// this window we give up on THIS attempt and let the supervisor's own retry
// loop (with backoff) handle it — see the reconnect note below.
const CONNECT_TIMEOUT_MS = 8000;

export async function openDeepgram(opts: {
  onFinal: (text: string) => void;
  onError?: (err: Error) => void;
}): Promise<DeepgramSession> {
  const dg = new DeepgramClient({ apiKey: config.deepgramKey });

  const socket = await dg.listen.v1.connect({
    model: "nova-3",
    encoding: "mulaw",
    sample_rate: 8000,
    interim_results: "true",
    smart_format: "true",
    punctuate: "true",
    Authorization: `Token ${config.deepgramKey}`,
    // CRITICAL: the SDK socket is a ReconnectingWebSocket that defaults to 30
    // reconnect attempts. When Deepgram (or the network path) briefly refused
    // connections, every failed openDeepgram silently hammered ~6 reconnects/s;
    // across all live calls this got our Fly egress IP rate-limited by Deepgram
    // and turned a transient blip into a ~1-hour transcription outage
    // (2026-07-07). We do our OWN retry with backoff at the supervisor level, so
    // disable the SDK's self-reconnect entirely and bound the initial connect.
    reconnectAttempts: 0,
    connectionTimeoutInSeconds: CONNECT_TIMEOUT_MS / 1000,
  });

  let msgCount = 0;
  socket.on("message", (msg) => {
    msgCount++;
    if (msgCount <= 3) {
      console.log(`[deepgram] msg #${msgCount}:`, JSON.stringify(msg).slice(0, 400));
    }
    if (msg.type !== "Results") return;
    if (!msg.is_final) return;
    const text = msg.channel?.alternatives?.[0]?.transcript?.trim();
    if (text) opts.onFinal(text);
  });
  socket.on("error", (err) => {
    console.error("[deepgram] error", err);
    opts.onError?.(err);
  });
  socket.on("close", () => console.log("[deepgram] socket closed"));

  socket.connect();
  // Belt-and-suspenders around connectionTimeoutInSeconds: never let a stuck
  // waitForOpen() hang the caller (which would leave the *80 leg never dialed).
  // On timeout/failure, tear the socket down so it can't linger and reconnect,
  // then throw so the supervisor treats this as a fast failure and backs off.
  try {
    await withTimeout(socket.waitForOpen(), CONNECT_TIMEOUT_MS);
  } catch (err) {
    try {
      socket.close();
    } catch {
      /* already gone */
    }
    throw new Error(
      `deepgram connect failed within ${CONNECT_TIMEOUT_MS}ms: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
  console.log("[deepgram] socket open, ready for audio");

  return {
    sendAudio(payload) {
      socket.sendMedia(payload);
    },
    async close() {
      socket.close();
    },
  };
}
