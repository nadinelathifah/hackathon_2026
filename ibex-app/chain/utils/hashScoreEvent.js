const { ethers } = require("ethers");

const DEFAULT_USER_SALT = "ibex-demo-user-salt-2026";

function getMockScoreEvent() {
  return {
    userId: "ibex-user-001",
    previousScore: 690,
    newScore: 735,
    scoreBand: "Low Risk",
    confidence: 0.82,
    timestamp: "2026-07-03T12:00:00Z",
    modelVersion: "ibex-credit-model-v1.0",
    positiveFactors: [
      "Stable income deposits",
      "Rent paid consistently",
      "Lower spending-to-income ratio"
    ],
    negativeFactors: [
      "Short UK financial history"
    ]
  };
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }

  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }

  return JSON.stringify(value);
}

function hashUtf8(value) {
  return ethers.keccak256(ethers.toUtf8Bytes(value));
}

function hashScoreEvent(scoreEvent = getMockScoreEvent(), userSalt = DEFAULT_USER_SALT) {
  const canonicalScoreEvent = canonicalJson(scoreEvent);

  return {
    scoreEvent,
    canonicalScoreEvent,
    userSalt,
    userHash: hashUtf8(`${scoreEvent.userId}:${userSalt}`),
    scoreEventHash: hashUtf8(canonicalScoreEvent),
    modelVersionHash: hashUtf8(scoreEvent.modelVersion)
  };
}

module.exports = {
  DEFAULT_USER_SALT,
  canonicalJson,
  getMockScoreEvent,
  hashScoreEvent,
  hashUtf8
};
