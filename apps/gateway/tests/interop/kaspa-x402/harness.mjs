// Interop harness: the OFFICIAL @kaspa-x402/client (pinned 1.0.1) paying a KasGate CONTRACT v2.10 402.
//
// Protocol (JSON lines): pytest writes commands to stdin, the harness answers on stdout.
//   {"cmd":"create","header":<PAYMENT-REQUIRED>,"url":...,"method":...,"body":...,"paymentIdentifier":...}
//     -> the client parses the envelope, selects the exact entry, computes requestHash and calls the funding
//        provider; the provider (the wallet) is delegated back to pytest with {"rpc":"payExactTransaction",...}
//        and pytest answers {"result":...}. Then -> {"ok":true,"signatureHeader":...,"accepted":...,...}
//   {"cmd":"settle","header":<PAYMENT-RESPONSE>} -> applySettlement(decodePaymentResponseHeader(header))
// Everything protocol-side (envelope parsing, requirement selection, request hash, payload assembly, header
// encoding, settlement validation) is the official client's code; only signing is the Python test wallet.
import readline from "node:readline";
import { DirectModeClient, MemoryChannelStore } from "@kaspa-x402/client";
import { decodePaymentResponseHeader, encodePaymentSignatureHeader } from "@kaspa-x402/core";

const lines = readline.createInterface({ input: process.stdin });
const iterator = lines[Symbol.asyncIterator]();
const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
async function next() {
  const { value, done } = await iterator.next();
  if (done) throw new Error("stdin closed");
  return JSON.parse(value);
}
async function rpc(name, params) {
  send({ rpc: name, params });
  const reply = await next();
  if (reply.error) throw new Error(reply.error);
  return reply.result;
}

const config = await next();
const fundingProvider = {
  networkId: config.network,
  sourceKind: "hot-wallet",
  getPublicIdentity: async () => ({ address: config.payerAddress }),
  payExactTransaction: (request) => rpc("payExactTransaction", request),
  finalizeExactPaymentAttempt: async () => {},
  getUtxos: async () => [],
  prepareEscrowDeposit: async () => { throw new Error("not used"); },
  prepareEscrowTopUp: async () => { throw new Error("not used"); },
};
const scripts = new Map(Object.entries(config.scripts));
const client = new DirectModeClient({
  addressCodec: {
    scriptPublicKeyForAddress: (address) => {
      const script = scripts.get(address);
      if (!script) throw new Error(`unknown address ${address}`);
      return script;
    },
  },
  fundingProvider,
  store: new MemoryChannelStore(),
  supportedNetworks: [config.network],
  confirmationThreshold: 30,
  authorizeExactPayment: ({ intentDigest }) => ({ intentDigest }),
});

let payment;
for (;;) {
  let command;
  try {
    command = await next();
  } catch {
    break;
  }
  try {
    if (command.cmd === "create") {
      payment = await client.createPayment(command.header, {
        url: command.url,
        method: command.method,
        ...(command.body !== undefined && command.body !== null ? { body: command.body } : {}),
        ...(command.paymentIdentifier ? { paymentIdentifier: command.paymentIdentifier } : {}),
      });
      send({
        ok: true,
        signatureHeader: encodePaymentSignatureHeader(payment.paymentPayload),
        accepted: payment.accepted,
        transactionId: payment.transactionId,
        requestHash: payment.paymentPayload.payload.requestHash,
      });
    } else if (command.cmd === "settle") {
      const result = await client.applySettlement(payment, decodePaymentResponseHeader(command.header));
      send({ ok: true, result: JSON.parse(JSON.stringify(result, (_, v) => (typeof v === "bigint" ? v.toString() : v))) });
    } else {
      send({ ok: false, error: `unknown command ${command.cmd}` });
    }
  } catch (error) {
    send({ ok: false, error: String(error?.code ?? ""), message: String(error?.message ?? error) });
  }
}
