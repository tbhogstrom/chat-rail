import { describe, it, expect, vi } from "vitest";
import { sendMonitorDtmf } from "./supervisor.js";

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
