import { describe, expect, it } from "vitest";
import { policyMarks } from "./policyAnnotations";
import type { Policy } from "../data/types";

describe("policyMarks", () => {
  it("returns one mark for rent_cap, at start_tick - 1 (0-based)", () => {
    const policies: Policy[] = [{ type: "rent_cap", district: "gracia", start_tick: 30 }];
    const marks = policyMarks(policies);
    expect(marks).toEqual([{ tickIndex: 29, label: "Rent cap · gracia" }]);
  });

  it("returns two marks for tourist_flat_ban (start and end)", () => {
    const policies: Policy[] = [{ type: "tourist_flat_ban", districts: "all", start_tick: 60, end_tick: 300 }];
    const marks = policyMarks(policies);
    expect(marks).toEqual([
      { tickIndex: 59, label: "HUT ban starts" },
      { tickIndex: 299, label: "HUT ban ends" },
    ]);
  });

  it("returns one mark each for new_transit_line and low_emission_zone", () => {
    const policies: Policy[] = [
      { type: "new_transit_line", districts: ["nou_barris"], start_tick: 120 },
      { type: "low_emission_zone", districts: ["eixample"], start_tick: 180 },
    ];
    const marks = policyMarks(policies);
    expect(marks).toEqual([
      { tickIndex: 119, label: "New line opens" },
      { tickIndex: 179, label: "LEZ starts" },
    ]);
  });

  it("clamps a tick 0 or negative start to index 0", () => {
    const policies: Policy[] = [{ type: "rent_cap", district: "gracia", start_tick: 0 }];
    expect(policyMarks(policies)).toEqual([{ tickIndex: 0, label: "Rent cap · gracia" }]);
  });

  it("returns an empty list for no policies", () => {
    expect(policyMarks([])).toEqual([]);
    expect(policyMarks(undefined)).toEqual([]);
    expect(policyMarks(null)).toEqual([]);
  });
});
