import "dotenv/config";

function required(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

export const config = {
  sip: {
    domain: required("SIP_INFO_DOMAIN"),
    outboundProxy: required("SIP_INFO_OUTBOUND_PROXY"),
    username: required("SIP_INFO_USERNAME"),
    password: required("SIP_INFO_PASSWORD"),
    authorizationId: required("SIP_INFO_AUTHORIZATION_ID"),
  },
  deepgramKey: required("DEEPGRAM_API_KEY"),
  redis: {
    url: required("KV_REST_API_URL"),
    token: required("KV_REST_API_TOKEN"),
  },
  bridge: {
    port: Number(process.env.BRIDGE_PORT || 8787),
    apiKey: required("BRIDGE_API_KEY"),
  },
};
