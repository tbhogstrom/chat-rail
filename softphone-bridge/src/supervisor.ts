import type Softphone from "ringcentral-softphone";
import { openDeepgram, type DeepgramSession } from "./deepgram.js";
import { appendTranscript, clearTranscript } from "./redis.js";

export interface Supervisor {
  sessionId: string;
  stop(): Promise<void>;
}

export async function superviseCall(
  softphone: Softphone,
  sessionId: string,
  agentExtNumber: string,
): Promise<Supervisor> {
  console.log(`[sup:${sessionId}] starting — agent ext ${agentExtNumber}`);

  await clearTranscript(sessionId);

  const dg: DeepgramSession = await openDeepgram({
    onFinal: (text) => {
      console.log(`[sup:${sessionId}] final: ${text}`);
      void appendTranscript(sessionId, text);
    },
    onError: (err) => console.error(`[sup:${sessionId}] deepgram error`, err),
  });

  const callSession = await softphone.call("*80");

  let stopped = false;
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    try {
      await callSession.hangup();
    } catch (e) {
      /* may already be gone */
    }
    await dg.close();
    console.log(`[sup:${sessionId}] stopped`);
  };

  callSession.once("answered", async () => {
    await callSession.sendDTMFs(`${agentExtNumber}#`, 500);
    console.log(`[sup:${sessionId}] monitoring active`);
  });

  callSession.on("audioPacket", (...args: unknown[]) => {
    const rtp = args[0] as { payload: Buffer };
    dg.sendAudio(rtp.payload);
  });

  callSession.once("disposed", () => {
    void stop();
  });

  callSession.once("busy", () => {
    console.warn(`[sup:${sessionId}] busy — supervision refused`);
    void stop();
  });

  return { sessionId, stop };
}
