import SoftphoneDefault from "ringcentral-softphone";
import { config } from "./config.js";
import { buildServer } from "./server.js";

// Interop: ringcentral-softphone ships CJS + ESM dists but its package.json
// lacks "type": "module", so Node's default import may return the CJS
// module.exports wrapper { default: [class], __esModule: true } instead of
// the class itself. Unwrap when that's the case.
const Softphone = (SoftphoneDefault as unknown as { default?: typeof SoftphoneDefault })
  .default ?? SoftphoneDefault;

// A single misbehaving call leg (e.g. a DTMF send on a torn-down *80 socket)
// must never take down the whole supervisor and kill transcription for every
// other call. Log and stay alive instead of letting Node exit.
process.on("unhandledRejection", (reason) => {
  console.error("[bridge] unhandledRejection (kept alive):", reason);
});
process.on("uncaughtException", (err) => {
  console.error("[bridge] uncaughtException (kept alive):", err);
});

const softphone = new Softphone({
  domain: config.sip.domain,
  outboundProxy: config.sip.outboundProxy,
  username: config.sip.username,
  password: config.sip.password,
  authorizationId: config.sip.authorizationId,
  codec: "PCMU/8000",
});

await softphone.register();
console.log("[bridge] softphone registered");

const server = buildServer(softphone, undefined, { watchdog: true });
await server.listen({ host: config.bridge.host, port: config.bridge.port });
console.log(`[bridge] HTTP listening on :${config.bridge.port}`);

async function shutdown() {
  console.log("[bridge] shutting down");
  await server.close();
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
