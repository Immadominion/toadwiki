# toadwiki.xyz

An independent public ledger of the **$TOAD ("The Toad Pepe") airdrop campaign wallet** on
Solana: every outbound transfer, valued in USD at the minute it landed, what each recipient did
next, and the posts that drove it.

The site's central claim is arithmetic, not trust:

```
 185,582,162.674358  received
− 21,588,555.000000  distributed   (177 transfers → 159 recipients)
= 163,993,607.674358  held         ← equals the live on-chain balance, residual exactly 0
```

If that residual is not exactly zero, **the build fails and nothing is published.** (Figures above
are a snapshot; the campaign is live and every number moves.)

Design: **Pond Arcade** — saturated pond green used as a field colour with dark ink on top,
yellow CTAs, floating white cards, the El Sapo Pepe mascot. Colour, type and spacing tokens are
closed sets defined at the top of `app/globals.css`; nothing outside them is used.

## Run it

```bash
npm install
npm run dev          # local on :3000
npm run build        # Next 16 / Turbopack
```

Live price and market cap are fetched client-side from the DexScreener public API. No keys ever
reach the client.

## Data pipeline

`data/model.json` is a build artifact. It is **constructed** from collected sources — the
builder never reads its own output, and raw captures land in `data/raw/` with a sha256 manifest
before anything is derived.

```bash
cp .env.example .env      # HELIUS_RPC, X_BEARER_TOKEN — server/build-time only

python3 scripts/collect/transfers.py   # ATA history → transfers + the invariant
python3 scripts/collect/holders.py     # holder set → balances, held %, concentration
python3 scripts/collect/ohlcv.py       # minute candles (GeckoTerminal + pump.fun cross-check)
python3 scripts/collect/social.py      # X enumeration + keyless hydration
python3 scripts/collect/identity.py    # token metadata, authorities, wallet-proof exhibit

python3 scripts/build_model.py         # construct model.json; enforces the invariant
node   scripts/build-brief.mjs        # fact sheet for the assistant (also runs on prebuild)
```

Note: only `transfers.py` accepts `--env-file`. The rest read the environment, so export it once
(`set -a; . ./.env; set +a`) before running the sequence. `build_model.py` fails closed if the
chain has moved past the collected ledger, so the collectors must be re-run before a deploy.

The build asserts, and exits non-zero on any failure: the balance invariant closes at residual
0; collected totals meet their floors; the two independent price sources agree within 2% median
divergence; the mint is owned by Token-2022; and the trace is not stale against the live
balance.

### Collection rules that are not optional

- Query the campaign wallet's **associated token account**, never the owner wallet — the owner's
  history is ~3.4× polluted with spam airdrops from strangers.
- Paginate to **exhaustion**. There is no signature cap. A 150-signature cap paginating
  newest-first is what previously discarded the oldest drops and lost 55% of the tokens.
- $TOAD is **Token-2022**, not classic SPL. An SPL-only parser returns nothing, silently.
- **Integer base units** everywhere. Reading float `uiAmount` corrupts values.
- Supply is read live — it is still burning. Never hardcode `1e9`.
- A transfer is a **sale** only with evidence: a pool destination or a DEX program in the
  transaction. Wallet-to-wallet movement is "reduced", not sold.

### Operational gotchas

- Helius `getHealth` returns `"ok"` even when the key is credit-exhausted; real calls return
  `-32429 max usage reached`. Never use it as a liveness check.
- The X API fails two different ways: `403 spend-cap-reached` (a dollar cap, fixable in the
  developer console) and `401 Unauthorized` (the token itself is invalid). Collectors degrade to
  keyless hydration and record which source actually served the data.

## Key facts

| | |
|---|---|
| Mint (authentic) | `A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump` |
| Token program | Token-2022 · 6 decimals · mint/freeze/update authorities all null |
| Supply | ~960.57M live, ~3.94% burned and still drifting |
| Campaign wallet | `FuP8dYQ…TnmD` — **attributed** to @mdudas at high confidence, never self-disclosed |
| Deployer | `5YRgrP3…Uzij` — recorded as `creator` on the pump.fun bonding curve |
| Official X | @eltoadpepe · site: eltoadpepe.fun |
| Lore | El Sapo Pepe, Argentine TV, 1988 — published as the token's own immutable on-chain metadata, not as sourced history |

## Ask the toad

The hero carries an ask box; every other page gets a launcher in the corner. Answers come from
Claude Haiku, and the model is shown exactly one thing: `data/brief.json`, a ~16KB plain-text
fact sheet regenerated from `data/model.json` by `scripts/build-brief.mjs` on every build
(`prebuild`). It cannot drift from what the pages say.

The prompt forbids stating any number not in that sheet, forbids price predictions and financial
advice outright, and forbids the word "sold" unless sales were actually detected. "I don't know"
is a correct answer and the model is told so.

Cost control, in order of how much it matters:

1. `DAILY_QUESTION_CAP` — the endpoint stops calling the API for the day. This is the only hard
   bound on the bill.
2. Prompt caching — the fact sheet is byte-identical every request, so bursts cost a fraction.
3. `max_tokens` 420, and a pinned Haiku model that is *not* read from the environment.
4. Per-IP limits — real, but best effort: state lives in one serverless instance's memory and
   resets on a cold start. `lib/limits.ts` documents this rather than pretending otherwise.

Extra questions and suggestions are paid in $TOAD. Payment is verified **on-chain** before
anything is granted — the transfer must be TOAD, land on `TIP_WALLET`, clear the minimum, and be
under two hours old, and each signature can be redeemed once. Same balance-delta method the
ledger itself uses. Nothing ever connects to a visitor's wallet.

Every runtime feature fails closed. With no `ANTHROPIC_API_KEY` the ask box says so; with no
`TIP_WALLET` the paid paths are switched off rather than showing an address nobody controls.

See `/methodology` on the site for how each number is derived and what this project
**cannot** know.

Independent community project. Not affiliated with @mdudas, the deployer, @eltoadpepe, or
pump.fun. There is no claim portal and this site will never ask you to connect a wallet. NFA.
