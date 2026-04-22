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

// IVR phrases that signal the *80 monitoring attempt failed and RC is about
// to play ~30s of prompts before hanging up. We abandon the attempt early.
const IVR_FAIL_PHRASES = [
  "could not be connected",
  "did not hear you",
];

// Prefix of the *80 IVR greeting we don't want in the saved transcript.
const IVR_GREETING_PHRASES = [
  "please enter the extension you wish to monitor",
  "followed by pound",
];

function isIvrGreeting(text: string): boolean {
  const lower = text.toLowerCase();
  return IVR_GREETING_PHRASES.some((p) => lower.includes(p));
}

function isIvrFailure(text: string): boolean {
  const lower = text.toLowerCase();
  return IVR_FAIL_PHRASES.some((p) => lower.includes(p));
}

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

    let bailOnNextDispose = false;
    const dg = await openDeepgram({
      onFinal: (text) => {
        if (isIvrGreeting(text)) {
          // Silent drop — don't pollute the saved transcript with RC's *80 menu.
          return;
        }
        if (isIvrFailure(text)) {
          console.log(`[sup:${sessionId}] IVR failure detected: "${text}" — bailing attempt ${attempt}`);
          bailOnNextDispose = true;
          // Hang up our *80 leg; disposed handler will run retry logic.
          void call.hangup().catch(() => {});
          return;
        }
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
      const reason = bailOnNextDispose ? "IVR-bail" : "RC-disposed";
      console.log(
        `[sup:${sessionId}] *80 ${reason} (attempt ${attempt}). args:`,
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
