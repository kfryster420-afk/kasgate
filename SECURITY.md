# Security policy

KasGate moves real money once it runs on Kaspa mainnet, so we take vulnerability reports seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting: go to the **Security** tab of this repository and choose **Report a vulnerability**. Include:

- what the problem is and which component it affects (gateway, web, buyer SDK, MCP server),
- steps or a proof of concept to reproduce it, ideally against Kaspa testnet-10,
- the commit you tested.

Never include real private keys, API keys or mainnet funds in a report. Use throw-away testnet keys.

We aim to acknowledge a report within 3 working days and to tell you what we plan to do within 14 days. Please give us a reasonable chance to fix the problem before you disclose it publicly. We are happy to credit you in the fix if you want.

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | Yes |
| Anything else | No |

KasGate is currently **testnet-verified only; mainnet has not launched**, and it has not had an independent security audit. Treat it accordingly.

## In scope

- Payment verification and settlement: tampered outputs, replay, front-running, double settlement, fee-split bypass.
- Authentication: wallet-signature login, API keys, sessions.
- The SSRF guard on merchant upstream URLs.
- Leaks of secrets (upstream headers, API keys, keys held by the SDK or MCP server).

## Out of scope

- Problems that need a compromised host, database or Redis.
- Denial of service by sheer traffic volume.
- Issues in third-party services (the Kaspa network, public nodes, wallets, price APIs).
