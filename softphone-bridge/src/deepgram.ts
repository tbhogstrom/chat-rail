import { DeepgramClient } from "@deepgram/sdk";
import { config } from "./config.js";

export interface DeepgramSession {
  sendAudio(payload: Buffer): void;
  close(): Promise<void>;
}

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
  });

  socket.on("message", (msg) => {
    if (msg.type !== "Results") return;
    if (!msg.is_final) return;
    const text = msg.channel?.alternatives?.[0]?.transcript?.trim();
    if (text) opts.onFinal(text);
  });
  socket.on("error", (err) => {
    opts.onError?.(err);
  });

  socket.connect();
  await socket.waitForOpen();

  return {
    sendAudio(payload) {
      socket.sendMedia(payload);
    },
    async close() {
      socket.close();
    },
  };
}
