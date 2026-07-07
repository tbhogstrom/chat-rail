// Process-wide liveness signal for the /health probe and the wedge watchdog.
//
// `markPipelineHealthy()` is called whenever the transcription pipeline
// demonstrably WORKS end-to-end (a *80 leg streamed an audio packet, which
// means SIP + the *80 leg + the Deepgram socket are all alive). The bridge is
// considered wedged when it has active sessions it is supposed to be
// transcribing but the pipeline has shown no sign of life within the staleness
// window — the exact state of the 2026-07-07 outage, where the process stayed
// up (SIP registered, HTTP serving) but never produced a transcript for ~1 hour
// because Deepgram connections were failing. Health/watchdog turns that into an
// automatic restart instead of a silent, hour-long dead pipeline.

let lastPipelineHealthyAt = Date.now();

export function markPipelineHealthy(): void {
  lastPipelineHealthyAt = Date.now();
}

export function msSincePipelineHealthy(): number {
  return Date.now() - lastPipelineHealthyAt;
}
