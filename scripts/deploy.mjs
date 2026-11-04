// Deploy a contract file to Studionet with a local key (DEPLOYER_PK in .env, created if missing).
//   node scripts/deploy.mjs contracts/upbit_notice_market.py
// Prints the contract address once the deploy is ACCEPTED and the leader succeeded.
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import fs from "node:fs";

const file = process.argv[2];
const GEN = 10n ** 18n;
const RPC = studionet.rpcUrls.default.http[0];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function key(name) {
  const env = fs.existsSync(".env") ? fs.readFileSync(".env", "utf8") : "";
  const m = env.match(new RegExp(`^${name}=(0x[0-9a-f]+)`, "m"));
  if (m) return m[1];
  const pk = generatePrivateKey();
  fs.appendFileSync(".env", `${name}=${pk}\n`);
  return pk;
}

// Studionet sometimes answers with an HTML gateway page instead of JSON: retry those.
async function retry(label, fn, tries = 6) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (i >= tries) throw e;
      console.log(`  ${label} failed (${e.shortMessage ?? e.message}), retry ${i}`);
      await sleep(10_000 * i);
    }
  }
}

const account = createAccount(key("DEPLOYER_PK"));
const client = createClient({ chain: studionet, account });
await retry("fund", async () => {
  const res = await fetch(RPC, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sim_fundAccount", params: [account.address, Number(100n * GEN)] }),
  });
  const body = JSON.parse(await res.text());
  if (body.error) throw new Error(JSON.stringify(body.error));
});

const code = fs.readFileSync(file, "utf8");
const hash = await retry("deploy", () => client.deployContract({ account, code, args: [] }));
console.log(`deploy tx ${hash}`);
const r = await retry("receipt", () =>
  client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: 200, interval: 5000 }),
);
const leader = r?.consensus_data?.leader_receipt?.[0];
const address = r?.data?.contract_address ?? r?.to_address;
console.log(`status ${r?.status_name} leader ${leader?.execution_result} ${leader?.genvm_result?.error_code ?? ""}`);
if (leader?.execution_result !== "SUCCESS") process.exit(1);
console.log(`address ${address}`);
