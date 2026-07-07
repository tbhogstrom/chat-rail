import { describe, it, expect, vi } from "vitest";
import {
  sendMonitorDtmf,
  decideNextAction,
  BASE_RETRY_MS,
  MAX_BACKOFF_MS,
} from "./supervisor.js";

describe("decideNextAction", () => {
  // A healthy *80 leg: streamed real audio for minutes, then RC's duration cap
  // (or the leg dropping) disposed it. This is NOT a failure — retry promptly.
  const healthyLeg = {
    sourceActive: true,
    reason: "RC-disposed" as const,
    audioPackets: 45000,
    attemptMs: 900_000,
    prevConsecutiveFastFails: 0,
  };
  // A fast failure: *80 re-dial hit RC's IVR ("no call on this extension") and
  // bailed in ~10s having streamed only the ~470-packet menu.
  const ivrBail = {
    sourceActive: true,
    reason: "IVR-bail" as const,
    audioPackets: 471,
    attemptMs: 9700,
    prevConsecutiveFastFails: 0,
  };

  it("stops when the source call has ended", () => {
    const action = decideNextAction({ ...healthyLeg, sourceActive: false });
    expect(action.kind).toBe("stop");
  });

  it("NEVER stops while the source call is still live, no matter how many prior failures", () => {
    // Regression guard for the bug: MAX_ATTEMPTS=3 used to terminate a live call.
    const action = decideNextAction({ ...ivrBail, prevConsecutiveFastFails: 100 });
    expect(action.kind).toBe("retry");
  });

  it("retries a healthy capped leg promptly and resets the fast-fail counter", () => {
    const action = decideNextAction({ ...healthyLeg, prevConsecutiveFastFails: 4 });
    expect(action.kind).toBe("retry");
    expect(action.healthy).toBe(true);
    expect(action.delayMs).toBe(BASE_RETRY_MS);
    expect(action.consecutiveFastFails).toBe(0);
  });

  it("counts an IVR-bail as a fast failure and increments the counter", () => {
    const action = decideNextAction(ivrBail);
    expect(action.kind).toBe("retry");
    expect(action.healthy).toBe(false);
    expect(action.consecutiveFastFails).toBe(1);
    expect(action.delayMs).toBe(BASE_RETRY_MS);
  });

  it("counts an RC-disposed leg that streamed almost no audio as a fast failure", () => {
    const action = decideNextAction({
      ...healthyLeg,
      audioPackets: 40,
      attemptMs: 800,
    });
    expect(action.healthy).toBe(false);
    expect(action.consecutiveFastFails).toBe(1);
  });

  it("backs off exponentially across consecutive fast failures", () => {
    expect(decideNextAction({ ...ivrBail, prevConsecutiveFastFails: 0 }).delayMs).toBe(3000);
    expect(decideNextAction({ ...ivrBail, prevConsecutiveFastFails: 1 }).delayMs).toBe(6000);
    expect(decideNextAction({ ...ivrBail, prevConsecutiveFastFails: 2 }).delayMs).toBe(12000);
    expect(decideNextAction({ ...ivrBail, prevConsecutiveFastFails: 3 }).delayMs).toBe(24000);
  });

  it("caps the backoff at MAX_BACKOFF_MS", () => {
    const action = decideNextAction({ ...ivrBail, prevConsecutiveFastFails: 20 });
    expect(action.delayMs).toBe(MAX_BACKOFF_MS);
  });
});

describe("sendMonitorDtmf", () => {
  it("skips the send when the attempt is no longer active", async () => {
    const call = { sendDTMFs: vi.fn().mockResolvedValue(undefined) };
    const sent = await sendMonitorDtmf(call, "119#", () => false);
    expect(sent).toBe(false);
    expect(call.sendDTMFs).not.toHaveBeenCalled();
  });

  it("sends and returns true when the attempt is active", async () => {
    const call = { sendDTMFs: vi.fn().mockResolvedValue(undefined) };
    const sent = await sendMonitorDtmf(call, "119#", () => true);
    expect(sent).toBe(true);
    expect(call.sendDTMFs).toHaveBeenCalledWith("119#", 500);
  });

  it("does NOT throw when the *80 socket is torn down (regression: bridge crash)", async () => {
    const call = {
      sendDTMFs: vi.fn().mockRejectedValue(
        Object.assign(new Error("Not running"), {
          code: "ERR_SOCKET_DGRAM_NOT_RUNNING",
        }),
      ),
    };
    // Must RESOLVE false, never reject — an unhandled rejection here is exactly
    // what crashed the whole bridge and killed live transcription.
    await expect(sendMonitorDtmf(call, "119#", () => true)).resolves.toBe(false);
  });
});
