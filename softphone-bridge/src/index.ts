import Softphone from "ringcentral-softphone";
import { config } from "./config.js";

const softphone = new Softphone({
  domain: config.sip.domain,
  outboundProxy: config.sip.outboundProxy,
  username: config.sip.username,
  password: config.sip.password,
  authorizationId: config.sip.authorizationId,
  codec: "PCMU/8000",
});
softphone.enableDebugMode();

await softphone.register();
console.log("[bridge] softphone registered");

// Keep process alive
process.stdin.resume();
