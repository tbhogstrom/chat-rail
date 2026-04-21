import Softphone from "ringcentral-softphone";
import { config } from "./config.js";
import { buildServer } from "./server.js";

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

const server = buildServer(softphone);
await server.listen({ host: "0.0.0.0", port: config.bridge.port });
console.log(`[bridge] HTTP listening on :${config.bridge.port}`);

async function shutdown() {
  console.log("[bridge] shutting down");
  await server.close();
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
