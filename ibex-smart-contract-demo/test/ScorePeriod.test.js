const { expect } = require("chai");
const {
  formatScorePeriod,
  parseScorePeriod,
  resolveScorePeriod,
  scorePeriodFromTimestamp
} = require("../utils/scorePeriod");

describe("Score period utility", function () {
  it("parses and formats a YYYYMM score period", function () {
    expect(parseScorePeriod("202608")).to.equal(202608);
    expect(formatScorePeriod(202608)).to.equal("2026-08");
  });

  it("derives the score period from a UTC timestamp", function () {
    expect(scorePeriodFromTimestamp("2026-08-31T23:59:59Z")).to.equal(202608);
    expect(scorePeriodFromTimestamp("2026-09-01T00:00:00+01:00")).to.equal(202608);
  });

  it("uses an explicit configured period when provided", function () {
    const event = { timestamp: "2026-07-03T12:00:00Z" };

    expect(resolveScorePeriod(event, "202609")).to.equal(202609);
    expect(resolveScorePeriod(event, undefined)).to.equal(202607);
  });

  it("rejects malformed score periods and timestamps", function () {
    for (const invalidPeriod of ["20268", "202600", "202613", "not-a-period"]) {
      expect(() => parseScorePeriod(invalidPeriod)).to.throw();
    }

    expect(() => scorePeriodFromTimestamp("not-a-date")).to.throw();
  });
});
