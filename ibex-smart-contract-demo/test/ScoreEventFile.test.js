const { expect } = require("chai");
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  MIN_USER_SALT_LENGTH,
  loadScoreEvent,
  validateScoreEvent
} = require("../utils/loadScoreEvent");

describe("Score event file integration", function () {
  let tempDirectory;

  const validScoreEvent = {
    userId: "ibex-test-user-002",
    previousScore: 690,
    newScore: 735,
    scoreBand: "Low Risk",
    confidence: 0.82,
    timestamp: "2026-07-30T14:00:00.000Z",
    modelVersion: "ibex-credit-model-v1.0",
    positiveFactors: ["Stable income deposits"],
    negativeFactors: ["Short UK financial history"]
  };

  beforeEach(function () {
    tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "ibex-score-event-"));
  });

  afterEach(function () {
    fs.rmSync(tempDirectory, { recursive: true, force: true });
  });

  function writeFile(name, contents) {
    const filePath = path.join(tempDirectory, name);
    fs.writeFileSync(filePath, contents);
    return filePath;
  }

  it("uses the built-in mock event when no file is configured", function () {
    const result = loadScoreEvent(null, null);

    expect(result.isCustomEvent).to.equal(false);
    expect(result.scoreEvent.userId).to.equal("ibex-user-001");
  });

  it("loads and validates a custom score event", function () {
    const filePath = writeFile("score-event.json", JSON.stringify(validScoreEvent));
    const userSalt = "a".repeat(MIN_USER_SALT_LENGTH);
    const result = loadScoreEvent(filePath, userSalt);

    expect(result.isCustomEvent).to.equal(true);
    expect(result.scoreEvent).to.deep.equal(validScoreEvent);
    expect(result.userSalt).to.equal(userSalt);
  });

  it("requires a strong user salt for custom events", function () {
    const filePath = writeFile("score-event.json", JSON.stringify(validScoreEvent));

    expect(() => loadScoreEvent(filePath, "too-short")).to.throw(
      `USER_SALT must contain at least ${MIN_USER_SALT_LENGTH} characters`
    );
  });

  it("rejects invalid JSON", function () {
    const filePath = writeFile("score-event.json", "{not-json}");

    expect(() => loadScoreEvent(filePath, "a".repeat(MIN_USER_SALT_LENGTH))).to.throw(
      "Score event file is not valid JSON"
    );
  });

  it("rejects an incomplete score event", function () {
    expect(() => validateScoreEvent({ userId: "test" })).to.throw(
      'Score event field "modelVersion" must be a non-empty string'
    );
  });

  it("rejects an invalid score", function () {
    expect(() => validateScoreEvent({ ...validScoreEvent, newScore: "735" })).to.throw(
      'Score event field "newScore" must be a finite number'
    );
  });
});
