"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

const MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump";

type Live = {
  price: number | null;
  mcap: number | null;
  chg24: number | null;
  liquidity: number | null;
};

const LiveCtx = createContext<Live | null>(null);

export function useLive() {
  return useContext(LiveCtx);
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [live, setLive] = useState<Live | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const res = await fetch(
          `https://api.dexscreener.com/latest/dex/tokens/${MINT}`,
          { cache: "no-store" }
        );
        if (!res.ok) return;
        const data = await res.json();
        const pair = (data.pairs || []).sort(
          (a: any, b: any) => (b.liquidity?.usd || 0) - (a.liquidity?.usd || 0)
        )[0];
        if (!pair || cancelled) return;
        setLive({
          price: pair.priceUsd ? Number(pair.priceUsd) : null,
          mcap: pair.fdv ?? pair.marketCap ?? null,
          chg24: pair.priceChange?.h24 ?? null,
          liquidity: pair.liquidity?.usd ?? null,
        });
      } catch {
        /* silent — live price is optional */
      }
    }
    pull();
    const id = setInterval(pull, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return <LiveCtx.Provider value={live}>{children}</LiveCtx.Provider>;
}
