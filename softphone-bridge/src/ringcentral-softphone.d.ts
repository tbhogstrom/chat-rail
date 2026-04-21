// Shim for ringcentral-softphone v1.3.2: its own .d.ts imports `node:events`
// via default-import, which under NodeNext + its missing package.json
// `type: module` marker breaks tsc's view of its default export shape.
// We re-declare only the surface we use.
declare module "ringcentral-softphone" {
  type Codec = "OPUS/16000" | "OPUS/48000/2" | "PCMU/8000";
  export type SoftPhoneOptions = {
    domain: string;
    outboundProxy: string;
    username: string;
    password: string;
    authorizationId: string;
    codec?: Codec;
    ignoreTlsCertErrors?: boolean;
  };
  export default class Softphone {
    constructor(options: SoftPhoneOptions);
    register(): Promise<void>;
    enableDebugMode(): void;
    revoke(): void;
    on(event: string, listener: (...args: unknown[]) => void): this;
  }
}
