import { describe, expect, it } from "vitest";
import {
  districtDisplayName,
  formatCurrencyCompact,
  formatDateHuman,
  formatInt,
  formatPercent,
  formatTokensCompact,
  formatUsdCost,
} from "./format";

describe("formatInt", () => {
  it("formats with thousands separators and rounds", () => {
    expect(formatInt(1234.6)).toBe("1,235");
    expect(formatInt(0)).toBe("0");
  });
});

describe("formatPercent", () => {
  it("converts a fraction to a percent string", () => {
    expect(formatPercent(0.1234)).toBe("12.3%");
    expect(formatPercent(0.5, 0)).toBe("50%");
  });
});

describe("formatCurrencyCompact", () => {
  it("compacts thousands with a k suffix", () => {
    expect(formatCurrencyCompact(1234)).toBe("€1.2k");
  });

  it("keeps small values uncompacted", () => {
    expect(formatCurrencyCompact(340)).toBe("€340");
  });
});

describe("formatUsdCost", () => {
  it("marks estimated cost with 'est.'", () => {
    expect(formatUsdCost(1.5, true)).toBe("$1.50 est.");
  });

  it("omits the est. marker for real usage", () => {
    expect(formatUsdCost(1.5, false)).toBe("$1.50");
  });

  it("uses more precision for sub-dollar costs", () => {
    expect(formatUsdCost(0.0042, true)).toBe("$0.0042 est.");
  });
});

describe("formatTokensCompact", () => {
  it("compacts millions and thousands", () => {
    expect(formatTokensCompact(1_234_567)).toBe("1.2M");
    expect(formatTokensCompact(45_000)).toBe("45.0k");
    expect(formatTokensCompact(500)).toBe("500");
  });
});

describe("formatDateHuman", () => {
  it("formats an ISO date as 'D Mon YYYY'", () => {
    expect(formatDateHuman("2026-03-14")).toBe("14 Mar 2026");
  });
});

describe("districtDisplayName", () => {
  it("maps known slugs to accented display names", () => {
    expect(districtDisplayName("gracia")).toBe("Gràcia");
    expect(districtDisplayName("sant_marti")).toBe("Sant Martí");
  });

  it("falls back to the id for unknown slugs", () => {
    expect(districtDisplayName("unknown_district")).toBe("unknown_district");
  });
});
