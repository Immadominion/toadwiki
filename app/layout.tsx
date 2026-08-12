import type { Metadata } from "next";
import { Nunito, JetBrains_Mono, Press_Start_2P } from "next/font/google";
import { LiveProvider } from "@/components/live";
import { Nav } from "@/components/nav";
import { Footer } from "@/components/footer";
import { AskMount } from "@/components/ask-mount";
import { loadModel } from "@/lib/model";
import "./globals.css";

// Self-hosted and inlined by next/font, which removes the render-blocking
// third-party @import that used to sit on line 1 of globals.css.
const body = Nunito({
  subsets: ["latin"],
  weight: ["400", "600", "700", "800"],
  variable: "--f-body",
  display: "swap",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--f-mono",
  display: "swap",
});
const pixel = Press_Start_2P({
  subsets: ["latin"],
  weight: "400",
  variable: "--f-pixel",
  display: "swap",
});

const DESCRIPTION =
  "An independent ledger of the $TOAD airdrop campaign wallet: every onchain transfer, valued in USD at the minute it landed, with what each recipient did next.";

export const metadata: Metadata = {
  metadataBase: new URL("https://toadwiki.xyz"),
  title: "toadwiki.xyz · every $TOAD airdrop, receipts attached",
  description: DESCRIPTION,
  icons: {
    icon: [{ url: "/toad-icon.png", type: "image/png" }],
    apple: [{ url: "/toad-512.png", type: "image/png" }],
  },
  openGraph: {
    title: "toadwiki.xyz",
    description: DESCRIPTION,
    url: "https://toadwiki.xyz",
    siteName: "toadwiki.xyz",
    images: ["/toad-512.png"],
  },
  twitter: {
    card: "summary_large_image",
    title: "toadwiki.xyz",
    description: DESCRIPTION,
    images: ["/toad-512.png"],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const model = loadModel();
  return (
    <html lang="en" className={`${body.variable} ${mono.variable} ${pixel.variable}`}>
      <body>
        <LiveProvider>
          <Nav />
          <main>{children}</main>
          <Footer model={model} />
          <AskMount />
        </LiveProvider>
      </body>
    </html>
  );
}
