// Live run on Studionet against the real funding history of Binance, OKX, Gate,
// KuCoin and Hyperliquid.
//   node scripts/live_demo.mjs <contractAddress> [resume]
// 1) fix(): the median for real past windows, each picked for a reason
// 2) swaps: one proposed and accepted for the next 8h funding period, one cancelled,
//    one closed by both sides before it starts
// 3) waits for the period to end and settles it
// Maker, taker and keeper are demo keys. Results go to demo_result.json;
// "resume" continues from it after a failure (or after the wait was interrupted).
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import fs from "node:fs";

const [address, mode] = process.argv.slice(2);
const GEN = 10n ** 18n;
const OUT = "demo_result.json";
const BASE = 28_800;

function key(name) {
  const env = fs.existsSync(".env") ? fs.readFileSync(".env", "utf8") : "";
  const m = env.match(new RegExp(`^${name}=(0x[0-9a-f]+)`, "m"));
  if (m) return m[1];
  const pk = generatePrivateKey();
  fs.appendFileSync(".env", `${name}=${pk}\n`);
  return pk;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Math.floor(Date.now() / 1000);

// Studionet sometimes answers with an HTML error page instead of JSON, and allows
// about 30 requests a minute: retry with a growing pause, and pace every call.
async function retry(label, fn, tries = 6) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (i >= tries) throw e;
      console.log(`  ${label} failed (${(e.shortMessage ?? e.message).slice(0, 120)}), retry ${i}`);
      await sleep(15_000 * i);
    }
  }
}
async function fund(addr) {
  await retry("fund", async () => {
    const res = await fetch(studionet.rpcUrls.default.http[0], {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sim_fundAccount", params: [addr, Number(1000n * GEN)] }),
    });
    const body = JSON.parse(await res.text());
    if (body.error) throw new Error(JSON.stringify(body.error));
  });
}

const maker = createAccount(key("DEMO_MAKER_PK"));
const taker = createAccount(key("DEMO_TAKER_PK"));
const keeper = createAccount(key("DEMO_KEEPER_PK"));
const gl = createClient({ chain: studionet });

const plain = (v) => JSON.parse(JSON.stringify(v, (_, x) => (typeof x === "bigint" ? x.toString() : x instanceof Map ? Object.fromEntries(x) : x)));
const read = (functionName, args) =>
  retry(`read ${functionName}`, async () =>
    plain(await gl.readContract({ address, functionName, args, transactionHashVariant: "latest-nonfinal" })));

// A write can come back ACCEPTED while the leader errored (validators agreed on the error),
// so log the leader's execution result and the votes, not just the status. There is no LLM
// in this contract, but LLM_RATE_LIMITED is retried like everywhere else.
async function write(account, functionName, args, value = 0n) {
  for (let attempt = 1; attempt <= 4; attempt++) {
    await sleep(6_000);
    const hash = await retry(`send ${functionName}`, () => gl.writeContract({ account, address, functionName, args, value }));
    const r = await retry(`receipt ${hash}`, () =>
      gl.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: 120, interval: 8000 }));
    const leader = r?.consensus_data?.leader_receipt?.[0];
    const exec = leader?.execution_result;
    const err = leader?.genvm_result?.error_code;
    const votes = r?.last_round?.validator_votes_name ?? null;
    const payload = leader?.result?.payload;
    const msg = typeof payload === "string" ? payload : JSON.stringify(plain(payload ?? ""));
    console.log(`${functionName} -> ${hash} ${r?.status_name ?? ""} ${exec ?? ""} ${err ?? ""} ${votes ? votes.join(",") : ""}`);
    if (exec !== "ERROR" || err !== "LLM_RATE_LIMITED") {
      return { hash, status: r?.status_name, exec, error: err ?? null, votes,
               rotations_left: r?.last_round?.rotations_left ?? null, message: String(msg).slice(0, 600) };
    }
    await sleep(60_000 * attempt);
  }
  throw new Error(`${functionName} kept failing`);
}

const day = (iso) => Math.floor(Date.parse(iso + "T00:00:00Z") / 1000);
// [label, asset, from, to, why]
const FIXINGS = [
  ["btc-1014", "BTC", day("2026-10-14"), day("2026-10-15"), "Binance -0.44 bp vs Hyperliquid +2.79 bp on the same day"],
  ["btc-1011", "BTC", day("2026-10-11"), day("2026-10-12"), "a negative funding day"],
  ["btc-week", "BTC", day("2026-10-05"), day("2026-10-12"), "7-day window: 21 prints per 8h venue, 168 on Hyperliquid"],
  ["eth-1103", "ETH", day("2026-11-03"), day("2026-11-04"), "a second asset"],
  ["nope-1014", "NOPE", day("2026-10-14"), day("2026-10-15"), "an asset no venue lists"],
];

const resume = mode === "resume";
const out = resume ? JSON.parse(fs.readFileSync(OUT, "utf8"))
  : { contract: address, network: "studionet", created: now(), fixings: [], swaps: [] };
const save = () => fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
if (!resume) for (const a of [maker, taker, keeper]) await fund(a.address);

// 1) fixings on real past windows
for (const [label, asset, from, to, why] of FIXINGS) {
  if (out.fixings.some((f) => f.label === label)) continue;
  const tx = await write(keeper, "fix", [asset, from, to]);
  const stored = await read("get_fixing", [asset, from, to]);
  console.log(`  ${label}: median ${stored.median ?? "-"}`, stored.used ?? tx.message);
  out.fixings.push({ label, asset, from, to, why, tx, stored });
  save();
}

// 2) swaps on the next 8h funding period
const NOTIONAL = 10_000n * GEN;
if (!out.swaps.length) {
  const start = Math.ceil((now() + 600) / BASE) * BASE;
  out.start = start;
  // [label, maker pays fixed, fixed per 8h, cap per 8h]
  for (const [label, pays, fixed, cap] of [
    ["live", true, "0.0001", "0.0001"],
    ["cancelled", false, "0.00008", "0.0001"],
    ["closed-early", false, "0.00008", "0.0001"],
  ]) {
    const margin = BigInt(await read("quote_margin", [NOTIONAL, cap, BASE, 1]));
    const id = Number(await read("get_count", []));
    const tx = await write(maker, "propose", ["BTC", pays, fixed, cap, NOTIONAL, start, BASE, 1, ""], margin);
    out.swaps.push({ label, id, maker_pays_fixed: pays, fixed, cap, notional: NOTIONAL.toString(), margin: margin.toString(), propose: tx });
    save();
  }
}
const sw = (label) => out.swaps.find((s) => s.label === label);
for (const label of ["live", "closed-early"]) {
  const s = sw(label);
  if (!s.accept) { s.accept = await write(taker, "accept", [s.id], BigInt(s.margin)); save(); }
}
if (!sw("cancelled").cancel) { sw("cancelled").cancel = await write(maker, "cancel", [sw("cancelled").id]); save(); }
const ce = sw("closed-early");
if (!ce.close) {
  ce.close = [await write(maker, "request_close", [ce.id]), await write(taker, "request_close", [ce.id])];
  save();
}
for (const s of out.swaps) { s.state = await read("get_swap", [s.id]); console.log(`  ${s.label}: ${s.state.status}`); }
save();

// 3) settle the live swap after its period ends
const live = sw("live");
const due = out.start + BASE + 660;
if (!live.settle) {
  if (now() < due) { console.log(`waiting ${due - now()} s for the period to end`); await sleep((due - now()) * 1000); }
  live.settle = await write(keeper, "settle", [live.id]);
  live.period = await read("get_period", [live.id, 0]);
  live.final = await read("get_swap", [live.id]);
  live.fixing = await read("get_fixing", ["BTC", out.start, out.start + BASE]);
  console.log(`  live: ${JSON.stringify(live.period)}`);
  save();
}
console.log("done");
