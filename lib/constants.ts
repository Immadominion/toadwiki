/**
 * Chain constants. Single source of truth — the mint used to be re-typed in
 * five places across the app, which is exactly how a decoy address gets shipped.
 *
 * Every value here is verifiable on-chain; see /methodology.
 */

/** The one authentic $TOAD mint. Token-2022, 6 decimals. */
export const MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump";

/** Token-2022, NOT classic SPL. An SPL-only parser returns nothing, silently. */
export const TOKEN_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";

export const DECIMALS = 6;

/** The campaign wallet. Attributed to @mdudas at high confidence — see the
 *  evidence panel — but never self-disclosed by him. Label it, don't assert it. */
export const CAMPAIGN_WALLET = "FuP8dYQytaThMh9Fg2XNd1Z1eNHxMHW92kVUfWf3TnmD";

/** Its associated token account — the correct thing to paginate for transfer
 *  history. The owner wallet has 561 signatures polluted with spam airdrops;
 *  the ATA has the real 166. Querying the owner is what lost 55% of the data. */
export const CAMPAIGN_ATA = "AuA2VRui5JNWNWF79iyaSKpW7zMQLfzFZBjd2uS3YW2H";

/** Deployer. Proven: the pump.fun bonding-curve account stores this as `creator`. */
export const DEPLOYER = "5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij";

/** Canonical pricing pool: PumpSwap, constant-product, created 4s after the mint.
 *  Do NOT price off the Meteora DLMM pool — its reserve ratio is ~6.6x off spot. */
export const PRICING_POOL = "Nx9dcwNs3iJxM5YAxshMHE4aYJHdDyyGMhVcmaSgfu8";

export const BONDING_CURVE = "9oi3zoTqd1T8T3CVuSDfSNwjeWaj6zZLdYMLWNyayaeA";

export const solscanToken = (mint = MINT) => `https://solscan.io/token/${mint}`;
export const solscanAccount = (addr: string) => `https://solscan.io/account/${addr}`;
export const solscanTx = (sig: string) => `https://solscan.io/tx/${sig}`;
