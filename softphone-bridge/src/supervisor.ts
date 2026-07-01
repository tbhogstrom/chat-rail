import type Softphone from "ringcentral-softphone";
import type { CallSession } from "ringcentral-softphone";
import { openDeepgram, type DeepgramSession } from "./deepgram.js";
import { appendTranscript, getCallState } from "./redis.js";

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
  "no call on this extension",
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

/**
 * Send the monitoring DTMF (`<ext>#`) on the *80 leg, safely.
 *
 * The *80 leg can be disposed during the post-answer delay (RC caps *80
 * duration, or the source call ends). Sending DTMF on a torn-down UDP socket
 * throws ERR_SOCKET_DGRAM_NOT_RUNNING; because it runs inside an un-awaited
 * event listener, an unhandled rejection would crash the whole bridge and
 * kill live transcription for every subsequent call. This never throws:
 * returns true if sent, false if skipped (no longer active) or the send failed.
 */
export async function sendMonitorDtmf(
  call: { sendDTMFs: (dtmf: string, intervalMs: number) => Promise<void> },
  dtmf: string,
  isActive: () => boolean,
): Promise<boolean> {
  if (!isActive()) return false;
  try {
    await call.sendDTMFs(dtmf, 500);
    return true;
  } catch {
    return false;
  }
}

export async function superviseCall(
  softphone: Softphone,
  sessionId: string,
  agentExtNumber: string,
  onStopped?: () => void,
): Promise<Supervisor> {
  const startedAt = Date.now();
  // Intentionally do NOT clear the transcript here. If Python re-POSTs
  // start_supervision after our 3-attempt retry loop gives up (happens on
  // long calls where RC's *80 duration cap forces multiple fresh supervisor
  // cycles), we want to APPEND to the accumulated transcript, not wipe it.
  // For brand-new calls, the Redis key simply doesn't exist yet.

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
      // Only send if THIS attempt is still the live one — the *80 leg may have
      // been disposed (or superseded by a retry) during the 5s wait.
      const sent = await sendMonitorDtmf(
        call,
        `${agentExtNumber}#`,
        () => state === "running" && current?.call === call,
      );
      console.log(
        sent
          ? `[sup:${sessionId}] monitoring active (attempt ${attempt})`
          : `[sup:${sessionId}] monitoring DTMF skipped — *80 leg gone (attempt ${attempt})`,
      );
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
