"use client";

import { usePathname } from "next/navigation";
import { Ask } from "./ask";

/**
 * Mounts the floating asker everywhere except the overview, where the hero
 * already carries one — two ask boxes on one screen would read as a bug.
 * Also stays off /support, which is about giving rather than asking.
 */
export function AskMount() {
  const path = usePathname();
  const home = path === "/" || path === "";
  const support = path?.startsWith("/support");
  if (home || support) return null;
  return <Ask variant="dock" />;
}
