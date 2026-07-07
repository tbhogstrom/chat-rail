import type Softphone from "ringcentral-softphone";
import type { CallSession } from "ringcentral-softphone";
import { openDeepgram, type DeepgramSession } from "./deepgram.js";
import { appendTranscript, getCallState, logSupEvent } from "./redis.js";
import { markPipelineHealthy } from "./liveness.js";

export interface Supervisor {
  sessionId: string;
  stop(): Promise<void>;
}

const TERMINAL_STATUSES = new Set(["Disconnected", "Gone", "VoiceMail"]);

// Retry policy for re-establishing the *80 monitor leg after it's disposed.
// We NEVER give up while the source call is still live — RC caps *80 leg
// duration and occasionally answers a re-dial with an IVR failure, so a long
// call legitimately needs many fresh legs. The old MAX_ATTEMPTS=3 hard cap
// terminated recording mid-call (observed in production: 3 IVR-bails ->
// max_attempts -> stopped while source status was still "Answered"). Instead we
// retry indefinitely with exponential backoff, and only *consecutive fast
// failures* grow the delay — a healthy leg that streamed real audio resets it.
export const BASE_RETRY_MS = 3000;
export const MAX_BACKOFF_MS = 30000;
// A leg that streamed at least this many RTP packets (~20s at 50 pkt/s) counted
// as real monitored audio; below this (e.g. the ~470-packet *80 IVR menu) it's
// a fast failure. PCMU/8000 in 160B frames = 20ms/packet.
export const HEALTHY_AUDIO_PACKETS = 1000;

export interface AttemptOutcome {
  /** Is the source call still live (not Disconnected/Gone/VoiceMail)? */
  sourceActive: boolean;
  /** Why the *80 leg went away. */
  reason: "RC-disposed" | "IVR-bail";
  /** RTP packets streamed to Deepgram during this attempt. */
  audioPackets: number;
  /** Wall-clock duration of this attempt (dial + monitor), ms. */
  attemptMs: number;
  /** Consecutive fast failures BEFORE this attempt. */
  prevConsecutiveFastFails: number;
}

export interface NextAction {
  kind: "retry" | "stop";
  /** True if this attempt streamed real audio (healthy leg RC capped). */
  healthy: boolean;
  /** Updated consecutive-fast-failure count to carry into the next attempt. */
  consecutiveFastFails: number;
  /** Delay before the next attempt (only meaningful when kind === "retry"). */
  delayMs?: number;
}

/**
 * Decide what to do after a *80 monitor leg is disposed. Pure/synchronous so
 * the retry policy is unit-testable in isolation from softphone/Deepgram/Redis.
 */
export function decideNextAction(o: AttemptOutcome): NextAction {
  const healthy = o.reason !== "IVR-bail" && o.audioPackets >= HEALTHY_AUDIO_PACKETS;
  const consecutiveFastFails = healthy ? 0 : o.prevConsecutiveFastFails + 1;

  // Only the source call ending stops supervision — never the attempt count.
  if (!o.sourceActive) {
    return { kind: "stop", healthy, consecutiveFastFails };
  }

  const delayMs = healthy
    ? BASE_RETRY_MS
    : Math.min(BASE_RETRY_MS * 2 ** (consecutiveFastFails - 1), MAX_BACKOFF_MS);
  return { kind: "retry", healthy, consecutiveFastFails, delayMs };
}

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
  // start_supervision (e.g. a WS reconnect/snapshot re-hydration mid-call), we
  // want to APPEND to the accumulated transcript, not wipe it. For brand-new
  // calls, the Redis key simply doesn't exist yet.

  type State = "running" | "retrying" | "stopped";
  let state: State = "running";
  let attempt = 0;
  // Consecutive fast failures (IVR-bails / legs with ~no audio). Grows the retry
  // backoff; reset to 0 by any healthy leg. Carried across attempts.
  let consecutiveFastFails = 0;
  let current: { dg: DeepgramSession; call: CallSession } | null = null;

  const fireStopped = () => {
    if (state === "stopped") return;
    state = "stopped";
    const ms = Date.now() - startedAt;
    console.log(`[sup:${sessionId}] stopped after ${ms}ms across ${attempt} attempt(s)`);
    void logSupEvent(sessionId, "stopped", { attempts: attempt, totalMs: ms });
    onStopped?.();
  };

  const startAttempt = async (): Promise<void> => {
    attempt++;
    state = "running";
    const attemptStartedAt = Date.now();
    console.log(`[sup:${sessionId}] attempt ${attempt} (consecutiveFastFails=${consecutiveFastFails})`);
    void logSupEvent(sessionId, "attempt_start", { attempt, consecutiveFastFails });

    let bailOnNextDispose = false;
    // Declared up-front so the onFinal closure can reference it; assigned once
    // the *80 leg is dialed (well before any transcript final can fire).
    let call: CallSession;

    let dg: DeepgramSession;
    try {
      dg = await openDeepgram({
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
    } catch (err) {
      // Deepgram wouldn't open (now fails fast instead of storming reconnects).
      // Treat as a fast failure so the retry loop backs off — never hang the
      // caller (POST /sessions) or kill supervision for a still-live call.
      console.error(`[sup:${sessionId}] deepgram open failed (attempt ${attempt})`, err);
      void logSupEvent(sessionId, "dg_open_failed", {
        attempt,
        error: err instanceof Error ? err.message : String(err),
      });
      void scheduleRetryOrStop({
        reason: "IVR-bail",
        audioPackets: 0,
        attemptMs: Date.now() - attemptStartedAt,
      });
      return;
    }

    try {
      call = await softphone.call("*80");
    } catch (err) {
      // *80 dial failed (e.g. SIP registration lapsed). Same handling: back off
      // and retry while the source call is live; don't leave the socket behind.
      console.error(`[sup:${sessionId}] *80 dial failed (attempt ${attempt})`, err);
      void logSupEvent(sessionId, "dial_failed", {
        attempt,
        error: err instanceof Error ? err.message : String(err),
      });
      await dg.close().catch(() => {});
      void scheduleRetryOrStop({
        reason: "IVR-bail",
        audioPackets: 0,
        attemptMs: Date.now() - attemptStartedAt,
      });
      return;
    }
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
      void logSupEvent(
        sessionId,
        sent ? "monitoring_active" : "monitoring_dtmf_skipped",
        { attempt },
      );
    });

    let audioPackets = 0;
    call.on("audioPacket", (...args: unknown[]) => {
      const rtp = args[0] as { payload: Buffer };
      audioPackets++;
      // End-to-end proof of life (SIP + *80 leg + Deepgram socket all working).
      // Feeds the /health probe + wedge watchdog so a dead pipeline auto-heals.
      markPipelineHealthy();
      if (audioPackets === 1) {
        // Timestamp when real media actually began for this attempt — lets us
        // measure true monitored-audio duration per *80 leg.
        void logSupEvent(sessionId, "first_audio", { attempt });
      }
      if (audioPackets === 1 || audioPackets % 250 === 0) {
        console.log(
          `[sup:${sessionId}] audio packets: ${audioPackets}, last size: ${rtp.payload.length}B`,
        );
      }
      dg.sendAudio(rtp.payload);
    });

    call.once("disposed", async (...args: unknown[]) => {
      const reason = bailOnNextDispose ? "IVR-bail" : "RC-disposed";
      const attemptMs = Date.now() - attemptStartedAt;
      console.log(
        `[sup:${sessionId}] *80 ${reason} (attempt ${attempt}). args:`,
        JSON.stringify(args).slice(0, 200),
      );
      await dg.close();
      current = null;
      // Key diagnostic record: did this *80 leg stream real audio for minutes
      // (healthy leg RC capped) or bail in seconds with ~no audio (IVR churn)?
      void logSupEvent(sessionId, "disposed", { attempt, reason, audioPackets, attemptMs });
      await scheduleRetryOrStop({ reason, audioPackets, attemptMs });
    });

    call.once("busy", () => {
      console.warn(`[sup:${sessionId}] busy — supervision refused`);
      void logSupEvent(sessionId, "busy", { attempt });
      void (async () => {
        await dg.close();
        current = null;
        fireStopped();
      })();
    });
  };

  // Decide what to do after a *80 leg ended (disposed) OR after we failed to
  // even establish one (Deepgram open / dial error): stop only if the source
  // call is gone, otherwise schedule a backed-off retry. Shared by both paths
  // so a setup failure gets the same "never give up on a live call" treatment
  // as a normal dispose. Never throws and never tight-loops (backoff via
  // decideNextAction), so a persistent failure retries at most every
  // MAX_BACKOFF_MS instead of storming.
  const scheduleRetryOrStop = async (outcome: {
    reason: "RC-disposed" | "IVR-bail";
    audioPackets: number;
    attemptMs: number;
  }): Promise<void> => {
    try {
      if (state === "stopped") return; // external stop already in progress

      const callState = await getCallState(sessionId);
      const sourceActive =
        callState != null && !TERMINAL_STATUSES.has(callState.status ?? "");

      const action = decideNextAction({
        sourceActive,
        reason: outcome.reason,
        audioPackets: outcome.audioPackets,
        attemptMs: outcome.attemptMs,
        prevConsecutiveFastFails: consecutiveFastFails,
      });
      consecutiveFastFails = action.consecutiveFastFails;

      // Only the source call ending stops us — never an attempt cap. As long as
      // the call is live we keep re-establishing the *80 leg (backing off on
      // consecutive fast failures) so recording never dies mid-call.
      if (action.kind === "stop") {
        console.log(
          `[sup:${sessionId}] source call ended (status=${callState?.status ?? "unknown"}); not retrying`,
        );
        void logSupEvent(sessionId, "source_ended", {
          attempt,
          sourceStatus: callState?.status ?? "unknown",
        });
        fireStopped();
        return;
      }

      const delayMs = action.delayMs ?? BASE_RETRY_MS;
      console.log(
        `[sup:${sessionId}] source still active (status=${callState?.status}); ` +
          `healthy=${action.healthy} consecutiveFastFails=${consecutiveFastFails}; retrying in ${delayMs}ms`,
      );
      void logSupEvent(sessionId, "retrying", {
        attempt,
        sourceStatus: callState?.status,
        healthy: action.healthy,
        consecutiveFastFails,
        delayMs,
      });
      state = "retrying";
      await new Promise((r) => setTimeout(r, delayMs));
      if ((state as State) !== "retrying") return; // external stop during wait
      await startAttempt();
    } catch (err) {
      // getCallState / other unexpected error. Don't crash the process and don't
      // tight-loop; the /health liveness signal + Python re-POST are the
      // backstop if we somehow can't recover on our own.
      console.error(`[sup:${sessionId}] scheduleRetryOrStop error`, err);
      void logSupEvent(sessionId, "retry_error", {
        attempt,
        error: err instanceof Error ? err.message : String(err),
      });
    }
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
    void logSupEvent(sessionId, "external_stop", {
      prevState: prev,
      attempts: attempt,
      totalMs: ms,
    });
    onStopped?.();
  };

  return { sessionId, stop: externalStop };
}
