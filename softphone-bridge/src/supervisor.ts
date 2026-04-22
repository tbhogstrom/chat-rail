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
  onStopped?: () => void,
): Promise<Supervisor> {
  console.log(`[sup:${sessionId}] starting — agent ext ${agentExtNumber}`);
  const startedAt = Date.now();

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
    const durationMs = Date.now() - startedAt;
    console.log(`[sup:${sessionId}] stopped after ${durationMs}ms`);
    onStopped?.();
  };

  callSession.once("answered", async () => {
    // RC's *80 IVR greets for a few seconds before it's ready to receive
    // DTMF. Sending digits immediately gets them dropped during the greeting.
    // Tyler Liu's rc-softphone-monitor-demo uses a 5s pre-DTMF wait.
    await new Promise((r) => setTimeout(r, 5000));
    await callSession.sendDTMFs(`${agentExtNumber}#`, 500);
    console.log(`[sup:${sessionId}] monitoring active`);
  });

  let audioPackets = 0;
  callSession.on("audioPacket", (...args: unknown[]) => {
    const rtp = args[0] as { payload: Buffer };
    audioPackets++;
    if (audioPackets === 1 || audioPackets % 250 === 0) {
      console.log(
        `[sup:${sessionId}] audio packets: ${audioPackets}, last size: ${rtp.payload.length}B`,
      );
    }
    dg.sendAudio(rtp.payload);
  });

  callSession.once("disposed", (...args: unknown[]) => {
    console.log(
      `[sup:${sessionId}] *80 call disposed by RC. args:`,
      JSON.stringify(args).slice(0, 300),
    );
    void stop();
  });

  callSession.once("busy", () => {
    console.warn(`[sup:${sessionId}] busy — supervision refused`);
    void stop();
  });

  return { sessionId, stop };
}
