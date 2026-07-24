import {
  Callout,
  Code,
  Grid,
  H1,
  H2,
  Link,
  Stack,
  Stat,
  Table,
  Text,
} from "cursor/canvas";

type Cand = {
  symbol: string;
  mint: string;
  liq: number;
  ageMin: number | null;
  source: "trending_pools" | "new_pools";
};

const CANDIDATES: Cand[] = [
  { symbol: "ANSEM", mint: "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", liq: 1905741, ageMin: 31193, source: "trending_pools" },
  { symbol: "Jimothy", mint: "Ge87EtsjwRQbHaqQmKRno69RFTwh9bfSsm99XNxTpump", liq: 296999, ageMin: 5050, source: "trending_pools" },
  { symbol: "three", mint: "FeMbDoX7R1Psc4GEcvJdsbNbZA3bfztcyDCatJVJpump", liq: 235423, ageMin: 117557, source: "trending_pools" },
  { symbol: "febu", mint: "4ko5tSr5o3H4v1sFtjTSd9MPUW7yx5AFCpkNPoL6pump", liq: 129372, ageMin: 15849, source: "trending_pools" },
  { symbol: "BULLCAT", mint: "G9j8WWDeJXZdvwQgP82ooDuHmpc3Gy8NCSins71Lpump", liq: 118897, ageMin: 7497, source: "trending_pools" },
  { symbol: "RISE", mint: "5zCypRD91xWpaQba5t2GG7QzEaJGTQzpYBZdeKuepump", liq: 100114, ageMin: 1818, source: "trending_pools" },
  { symbol: "Agamemnon", mint: "2cAtqsRafKS7baN3mvJARhyZiMRdW4fZYNUUWUrCpump", liq: 77525, ageMin: 856, source: "trending_pools" },
  { symbol: "Spain", mint: "6SWrSq9KgTRSeXmLBZ8Vodtv3DiaHKE2SU8PyoeAjGeX", liq: 55359, ageMin: 2, source: "new_pools" },
  { symbol: "WORLDCUP", mint: "33eum82LaAhtv5YkUq1BdwEviSErH5CnFxqVNLT5pump", liq: 46145, ageMin: 99468, source: "trending_pools" },
  { symbol: "MrSue", mint: "43mjozsTLTViop3seWnmgkcf1DRj6qMf1JWw7X8ypump", liq: 36691, ageMin: 1051, source: "trending_pools" },
  { symbol: "Mouse", mint: "3wwU3njdLCT3ebRUrTFeHwKG6zamiiCKwmpShSK2pump", liq: 35089, ageMin: 2408, source: "trending_pools" },
  { symbol: "ODYSSEUS", mint: "BoRwd4FuvPUTi8Mu4ViCBG6oe7i9PesjDMePEX4Tpump", liq: 22485, ageMin: 479, source: "trending_pools" },
  { symbol: "Spain", mint: "6KmTBPgi6xSHbQnaWtjtwFZzUvSu6aA2b7cymrygFSCA", liq: 15297, ageMin: 2, source: "new_pools" },
  { symbol: "LeysPepsi", mint: "DeudGGMQGXey9FB96BhmLMZ8iwHC9WzpcCHA8opnpump", liq: 14642, ageMin: 2, source: "new_pools" },
  { symbol: "HNUT", mint: "HbGVzMdUnxQu1dYcWFQYU3LboHF32rwY9Cem1WZ6pump", liq: 14376, ageMin: 693, source: "trending_pools" },
  { symbol: "BATH", mint: "6kK7vjq5o2D6f58x932Whqyin6TXSUUZvVYTvbpiweZd", liq: 12983, ageMin: 2, source: "new_pools" },
  { symbol: "Benny", mint: "2mGS6WWuDy1xNQuCo7irzH3iT4SsSeSaA3oJ5NLqpump", liq: 12887, ageMin: 1627, source: "trending_pools" },
  { symbol: "SUB", mint: "EPKPcUPmhcDfpRq1LtN46FuysHo49D5Q6W2L2oPmpump", liq: 12401, ageMin: 1506, source: "trending_pools" },
  { symbol: "ANSEMCOIN", mint: "5UNFUzBFu34Zv8Cewfn33FxbNtbFuihmXA4Rgee1pump", liq: 10974, ageMin: 778, source: "trending_pools" },
  { symbol: "POB", mint: "BAGWtYQm6kei9M9hoVcY3pqXxeiGZ1poGoq51io5mJS6", liq: 10525, ageMin: 1416, source: "trending_pools" },
  { symbol: "ARGENTINA", mint: "8vR5VdBAGAWVjvycijB4yLTRmJ3N8RaUS6m3jgAnpump", liq: 8237, ageMin: 98551, source: "trending_pools" },
  { symbol: "PRIETA", mint: "1UQECVt1gsVohirXZoGkkYyHzDdaRAf8end9LsQpump", liq: 7835, ageMin: 1232, source: "trending_pools" },
  { symbol: "Debt", mint: "4Do7pHhCid89bj1RqhV3GUAkrZhnE6acCFHMdcgxpump", liq: 4920, ageMin: 1081, source: "trending_pools" },
  { symbol: "Era", mint: "hj4Rze8QdsT1fQdA9BdApWniUiqa6YCBqvA5cWdpump", liq: 3355, ageMin: 2, source: "new_pools" },
  { symbol: "Cweam", mint: "Gijsou6b4WnAQZtT8XtJgf7MuDk65C4iv9YModRepump", liq: 2093, ageMin: 2, source: "new_pools" },
  { symbol: "BIDENS", mint: "6SAdgHJyMcNFwEUe2yK8Ru2SNWStQd18ym5p3c9Epump", liq: 1907, ageMin: 2, source: "new_pools" },
  { symbol: "BATH", mint: "FDiw9HwAFecFPBmtsmJXKEGe9S4f1HvLrQpD2kWbpump", liq: 1820, ageMin: 2, source: "new_pools" },
  { symbol: "Funeral", mint: "39GxvBvaPiSea37zMMVUFacH3askG1rqfrRCn5xgpump", liq: 1749, ageMin: 2, source: "new_pools" },
  { symbol: "BATH", mint: "2wHQK38pyCgxWd3BdyTEzmM3vZS5JK6jgdQJ1qGvpump", liq: 1739, ageMin: 2, source: "new_pools" },
  { symbol: "CUCU", mint: "DPCCCSLhjLP3reHuhBzCuX66c8LJLxQUo3Vq7xwmpump", liq: 1703, ageMin: 2, source: "new_pools" },
  { symbol: "messicat", mint: "GfMKs7iHtRXUxyoauezF87wDn4z9vKf1KuZZujNdpump", liq: 1690, ageMin: 2, source: "new_pools" },
  { symbol: "19", mint: "Cz59PT1zfAiKJY1aJoq4ZU8XpZ3EQ6iuHFh7KM4Epump", liq: 1688, ageMin: 2, source: "new_pools" },
  { symbol: "19", mint: "GyCq9fFmCHcYxKQx3pbpavgeCHGVsjqg8XQxcuPNpump", liq: 1688, ageMin: 2, source: "new_pools" },
  { symbol: "19", mint: "4BUdQ8vkCrG87JMAJpRCQhgRZGq9RM67tF1Ky2vupump", liq: 1688, ageMin: 2, source: "new_pools" },
  { symbol: "mensa", mint: "HyYeDXHZvqWb17TW9NdxCa4DTdS1qtLVaJkdWaAHpump", liq: 1423, ageMin: 2, source: "new_pools" },
  { symbol: "TBB", mint: "3yNa2r2J79jBc5BNmZCfTJvsTgNdma9wQbEcAyJSpump", liq: 717, ageMin: 2, source: "new_pools" },
  { symbol: "PESSI", mint: "3oooD3Y9ig9e4FCqjjyc2mmZjWBsLoZw55LpbJqupump", liq: 540, ageMin: 2, source: "new_pools" },
  { symbol: "GINGER", mint: "DrYwRxgHf5jPgQ6mTRMzBKcqZhxSobNzMsnLHQBnpump", liq: 406, ageMin: 2, source: "new_pools" },
  { symbol: "Spain", mint: "5Y1ZCN3hqnMLtH3iYmGEaXfSNxUnR5wEqKRphVn7HHtv", liq: 1, ageMin: 2, source: "new_pools" },
];

function fmtLiq(n: number): string {
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}k`;
  return `$${n.toFixed(0)}`;
}

function fmtAge(min: number | null): string {
  if (min == null) return "—";
  if (min < 60) return `${Math.round(min)}m`;
  if (min < 1440) return `${(min / 60).toFixed(1)}h`;
  return `${(min / 1440).toFixed(1)}d`;
}

export default function MemebotCandidates() {
  const trending = CANDIDATES.filter((c) => c.source === "trending_pools").length;
  const fresh = CANDIDATES.filter((c) => c.source === "new_pools").length;

  return (
    <Stack gap={20}>
      <H1>Scanner candidates</H1>
      <Text tone="secondary">
        Live GeckoTerminal pull (new + trending Solana pools). Contract address =
        mint. Dashboard’s “38” was from an earlier cycle; that list wasn’t kept in
        SQLite. This is a fresh discover: {CANDIDATES.length} unique mints.
      </Text>

      <Grid columns={3} gap={12}>
        <Stat value={String(CANDIDATES.length)} label="Unique mints" />
        <Stat value={String(trending)} label="Trending" />
        <Stat value={String(fresh)} label="New pools" />
      </Grid>

      <Callout tone="info">
        Tickers are not unique — several “Spain”, “BATH”, and “19” rows are
        different contracts. Always use the mint, not the name.
      </Callout>

      <H2>Tokens + contract (mint)</H2>
      <Text tone="tertiary">
        Source: GeckoTerminal · sorted by liquidity · Solscan links on each mint
      </Text>
      <Table
        stickyHeader
        striped
        headers={["Symbol", "Contract (mint)", "Liquidity", "Age", "Source"]}
        columnAlign={["left", "left", "right", "right", "left"]}
        rows={CANDIDATES.map((c) => [
          c.symbol,
          <Link href={`https://solscan.io/token/${c.mint}`}>
            <Code>{c.mint}</Code>
          </Link>,
          fmtLiq(c.liq),
          fmtAge(c.ageMin),
          c.source === "new_pools" ? "new" : "trending",
        ])}
      />
    </Stack>
  );
}
