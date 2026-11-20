# KasGate's own Kaspa node (rusty-kaspa `kaspad`)

The gateway talks to Kaspa over wRPC (Borsh) through the `kaspa` Python SDK. By default it uses the public
Resolver. Live testing on testnet-10 showed that about half of the nodes the Resolver hands out never answer. In
production, point `KASPA_RPC_URL` at a node you run yourself. This folder covers how.

* Compose service: `kaspad` in `infra/docker-compose.yml`, profile `node`. It is off unless you enable the profile.
* Image: `kaspanet/rusty-kaspad:v2.1.0@sha256:f85da74b9514584451f83a2cbea3aa93f700248302fe7e3e0ab17288f93c905b`
  (the multi-arch index for linux/amd64 and linux/arm64; the same digest is published as `supertypo/rusty-kaspad:v2.1.0`).
  * Built from <https://github.com/kaspanet/rusty-kaspa> tag `v2.1.0` (commit `01b532e8`). The image source is
    <https://github.com/supertypo/docker-rusty-kaspa>, as stated on the Docker Hub page
    <https://hub.docker.com/r/kaspanet/rusty-kaspad>. rusty-kaspa does not publish images from its own CI (its
    README only shows how to build `docker/Dockerfile.kaspad` yourself). `kaspanet/rusty-kaspad` is the image
    published under the `kaspanet` Docker Hub organisation.
  * Inside the image: Alpine 3.24, `kaspad 2.1.0` in `/app`, BusyBox `nc` (used by the healthcheck) and
    `entrypoint.sh`. The entrypoint chowns `/app/data` to uid 50051 and runs kaspad as user `kaspa`. It also looks up
    the host's public IP to advertise `--externalip`.
  * This matches the `serverVersion: 2.1.0` reported by the public testnet-10 nodes, and the SDK `kaspa==2.1.0`.
* `provision.sh`: one-shot setup for a fresh Ubuntu 24.04 host (Docker, ufw, systemd unit).

## Ports

Defaults come from rusty-kaspa v2.1.0, `consensus/core/src/network.rs`.

| Network     | P2P (open to the internet) | wRPC Borsh (gateway, internal only) | gRPC (loopback, unused) | wRPC JSON (off) |
|-------------|----------------------------|-------------------------------------|-------------------------|-----------------|
| mainnet     | 16111                      | 17110                               | 16110                   | 18110           |
| testnet-10  | 16211                      | 17210                               | 16210                   | 18210           |

* The Borsh port is only `expose`d on the compose network, never published to the host. Do not open it in the firewall.
* gRPC stays on kaspad's default, 127.0.0.1 inside the container. wRPC JSON is not enabled.

## Hardware

These figures come from the official rusty-kaspa node guide for v2.x, `docs/toccata-guide.md` at tag v2.1.0:
<https://github.com/kaspanet/rusty-kaspa/blob/v2.1.0/docs/toccata-guide.md#recommended-hardware-specifications>.

| | Minimum | Preferred |
|---|---|---|
| CPU | 8 cores | 12–16 cores |
| RAM | 16 GB | 32 GB |
| Disk | 640 GB SSD | 1 TB SSD |
| Network | ~80 Mbit/s (10 MB/s) | more |

* The guide sets these figures for mainnet at 10 BPS. Testnet-10 also runs at 10 BPS, and the guide doesn't give
  separate testnet figures. Plan for the same class of machine; its storage need is lower than mainnet's but unpublished.
* Use local SSD or NVMe. The measurement study behind these specs found that slow or network storage leads to
  stalls and out-of-memory kills during pruning. It also found that a 4 vCPU / 12 GiB host fails:
  <https://github.com/elldeeone/node-research/blob/main/investigations/node-resource-usage/REPORT.md>.
* Pruning is on by default (non-archival), and `--utxoindex` is required by the gateway.
* `KASPAD_RAM_SCALE`, which maps to kaspad's `--ram-scale` (range 0.1–10), scales kaspad's caches. Leave it at 1.0
  on 16 GB or more.

### Sync time

Neither rusty-kaspa nor its guides give an official initial block download (IBD) duration. The measurement study
above recorded direct IBD of about 1.4 h on an 8 vCPU / 16 GiB SSD cloud host, and 2.5–2.7 h when syncing through
a relay. It also recorded much longer sync on weak storage. Those runs were on a devnet that mirrors mainnet
settings. Real mainnet and testnet-10 IBD depends on peers and the size of the pruning window, so budget several
hours. The node is ready when `get_server_info` returns `isSynced: true` (see below). An open RPC port alone
doesn't mean the node is synced.

#### Finding: testnet-10 sync on the shared dev box (2026-11-20)

We tried a native kaspad v2.1.0 testnet-10 sync (`--utxoindex`) on the shared dev box (8 vCPU, 15 GB RAM, no swap).
The header stage (about 1.32M headers) took about 22–24 min both times. The UTXO-set download then never finished.
Run 1 had received more than 187M UTXOs after about 16 min when a box reboot killed it and corrupted the database.
Run 3 restarted the UTXO-set download four times, because periodic network stalls dropped every peer at once
(`timeout expired after 120s`), and each restart begins again from zero. Peak use was about 5.1 GB RSS and 11.7 GB
of disk. The UTXO-set stage needs a stable, uninterrupted connection for well over 16 min, and a clean shutdown on
reboot, so run the node on a VPS with the `kasgate-kaspad.service` systemd unit from `provision.sh`, not on a
shared or ephemeral machine.

## Firewall

Open only SSH and the P2P port for the chosen network: TCP 16111 on mainnet, TCP 16211 on testnet-10.
`provision.sh` sets this up with ufw. Docker publishes ports through its own iptables chains, which bypass ufw, so the
compose file publishes nothing except P2P. Postgres and Redis bind to 127.0.0.1, and the Borsh RPC port is internal only.

## Point the gateway at it

1. In `infra/.env`, set the following (testnet-10 shown; mainnet values in brackets):
   ```
   KASPAD_TESTNET=true           # [false]
   KASPAD_P2P_PORT=16211         # [16111]
   KASPAD_RPC_BORSH_PORT=17210   # [17110]
   KASPA_RPC_URL=ws://kaspad:17210   # [ws://kaspad:17110]
   ```
   `KASPA_NETWORK` must agree (`kaspa:testnet-10` or `kaspa:mainnet`). The engine connects with
   `RpcClient(url=KASPA_RPC_URL, encoding="borsh", network_id=...)`. It falls back to the public Resolver only when
   `KASPA_RPC_URL` is unset, empty or whitespace.
2. Run `docker compose --profile node up -d` in `infra/`. The gateway has no `depends_on` on kaspad, because a
   syncing node can take hours. The engine reconnects in the background, and payments get `503 rpc_unavailable`
   until the node answers.
3. Check sync progress without exposing RPC. This runs inside the gateway container on the compose network; use
   mainnet / 17110 on mainnet:
   ```
   docker compose exec -T gateway python - <<'PY'
   import asyncio
   from kaspa import NetworkId, RpcClient
   async def main() -> None:
       c = RpcClient(url="ws://kaspad:17210", encoding="borsh", network_id=NetworkId("testnet-10"))
       await c.connect(block_async_connect=True, timeout_duration=5000)
       info = await c.get_server_info()
       print("synced:", info["isSynced"], "utxoindex:", info["hasUtxoIndex"], "daa:", info["virtualDaaScore"])
       await c.disconnect()
   asyncio.run(main())
   PY
   ```
   Alternatively, watch `docker compose logs -f kaspad` for steady "Accepted block" lines.
   Until `isSynced` is true, leave `KASPA_RPC_URL` empty so the gateway keeps using the Resolver.

## Upgrades

1. Read the release notes at <https://github.com/kaspanet/rusty-kaspa/releases>. Hard-fork releases have a deadline:
   a node that isn't upgraded in time splits off the network.
2. Confirm the new tag and digest:
   ```
   curl -s "https://hub.docker.com/v2/repositories/kaspanet/rusty-kaspad/tags/<tag>" | python3 -c "import json,sys; print(json.load(sys.stdin)['digest'])"
   ```
   Bump `image:` in `docker-compose.yml` to `kaspanet/rusty-kaspad:<tag>@<digest>` in a reviewed commit.
3. Run `docker compose --profile node pull kaspad && docker compose --profile node up -d kaspad`.
   `stop_grace_period` is 2 minutes, so RocksDB can close cleanly.
4. Resync notes:
   * Database upgrades are one-way. The v2.x guide says you can't downgrade a migrated database; going back
     means a resync.
   * Switching networks (`KASPAD_TESTNET`) uses a separate subfolder of the same volume
     (`kaspa-mainnet/`, `kaspa-testnet-10/`), so neither network's data is overwritten.
   * To force a clean resync, stop kaspad, `docker volume rm kasgate_kaspad-data`, then start it again. Expect a full IBD.
5. Keep the `kaspa` Python SDK in `apps/gateway/requirements.txt` on a compatible release. The SDK and node
   versions were matched at 2.1.0 when this was written.

## Fees (for SDK and wallet authors)

The v2.x guide raises the minimum standard fee to 100 sompi per gram:
`fee >= 100 * max(compute_mass, 2 * tx_bytes)`. The SDK `Generator` already applies this. In the phase 4 live test, a
0.6 KAS payment paid 253,400 sompi.
