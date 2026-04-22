import type Softphone from "ringcentral-softphone";
import type { CallSession } from "ringcentral-softphone";
import { openDeepgram, type DeepgramSession } from "./deepgram.js";
import { appendTranscript, clearTranscript, getCallState } from "./redis.js";

export interface Supervisor {
  sessionId: string;
  stop(): Promise<void>;
}

const MAX_ATTEMPTS = 3;
const RETRY_DELAY_MS = 3000;
const TERMINAL_STATUSES = new Set(["Disconnected", "Gone", "VoiceMail"]);

export async function superviseCall(
  softphone: Softphone,
  sessionId: string,
  agentExtNumber: string,
  onStopped?: () => void,
): Promise<Supervisor> {
  const startedAt = Date.now();
  await clearTranscript(sessionId);

  type State = "running" | "retrying" | "stopped";
  let state: State = "running";
  let attempt = 0;
  let current: { dg: DeepgramSession; call: CallSession } | null = null;

  const fireStopped = () => {
    if (state === "stopped") return;
    state = "stopped";
    const ms = Date.now() - startedAt;
    console.log(`[sup:${sessionId}] stopped after ${ms}ms across ${attempt} attempt(s)`);
    onStopped?.();
  };

  const startAttempt = async (): Promise<void> => {
    attempt++;
    state = "running";
    console.log(`[sup:${sessionId}] attempt ${attempt}/${MAX_ATTEMPTS}`);

    const dg = await openDeepgram({
      onFinal: (text) => {
        console.log(`[sup:${sessionId}] final: ${text}`);
        void appendTranscript(sessionId, text);
      },
      onError: (err) => console.error(`[sup:${sessionId}] deepgram error`, err),
    });

    const call = await softphone.call("*80");
    current = { dg, call };

    call.once("answered", async () => {
      await new Promise((r) => setTimeout(r, 5000));
      await call.sendDTMFs(`${agentExtNumber}#`, 500);
      console.log(`[sup:${sessionId}] monitoring active (attempt ${attempt})`);
    });

    let audioPackets = 0;
    call.on("audioPacket", (...args: unknown[]) => {
      const rtp = args[0] as { payload: Buffer };
      audioPackets++;
      if (audioPackets === 1 || audioPackets % 250 === 0) {
        console.log(
          `[sup:${sessionId}] audio packets: ${audioPackets}, last size: ${rtp.payload.length}B`,
        );
      }
      dg.sendAudio(rtp.payload);
    });

    call.once("disposed", async (...args: unknown[]) => {
      console.log(
        `[sup:${sessionId}] *80 disposed (attempt ${attempt}). args:`,
        JSON.stringify(args).slice(0, 200),
      );
      await dg.close();
      current = null;

      if (state === "stopped") return; // external stop already in progress

      if (attempt >= MAX_ATTEMPTS) {
        console.log(`[sup:${sessionId}] max attempts (${MAX_ATTEMPTS}) reached`);
        fireStopped();
        return;
      }

      const callState = await getCallState(sessionId);
      const sourceActive =
        callState != null && !TERMINAL_STATUSES.has(callState.status ?? "");
      if (!sourceActive) {
        console.log(
          `[sup:${sessionId}] source call ended (status=${callState?.status ?? "unknown"}); not retrying`,
        );
        fireStopped();
        return;
      }

      console.log(
        `[sup:${sessionId}] source still active (status=${callState.status}); retrying in ${RETRY_DELAY_MS}ms`,
      );
      state = "retrying";
      await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
      if ((state as State) !== "retrying") return; // external stop during wait
      try {
        await startAttempt();
      } catch (err) {
        console.error(`[sup:${sessionId}] retry failed`, err);
        fireStopped();
      }
    });

    call.once("busy", () => {
      console.warn(`[sup:${sessionId}] busy — supervision refused`);
      void (async () => {
        await dg.close();
        current = null;
        fireStopped();
      })();
    });
  };

  await startAttempt();

  const externalStop = async () => {
    if (state === "stopped") return;
    const prev = state;
    state = "stopped";
    if (current) {
      try {
        await current.call.hangup();
      } catch (_e) {
        /* may be gone */
      }
      await current.dg.close();
      current = null;
    }
    const ms = Date.now() - startedAt;
    console.log(
      `[sup:${sessionId}] external stop (was ${prev}, after ${ms}ms, ${attempt} attempt(s))`,
    );
    onStopped?.();
  };

  return { sessionId, stop: externalStop };
}
