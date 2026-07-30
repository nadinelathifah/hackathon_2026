const fs = require("fs");
const path = require("path");
const { DEFAULT_USER_SALT, getMockScoreEvent } = require("./hashScoreEvent");

const MIN_USER_SALT_LENGTH = 16;

function requireNonEmptyString(scoreEvent, field) {
  if (typeof scoreEvent[field] !== "string" || scoreEvent[field].trim() === "") {
    throw new Error(`Score event field "${field}" must be a non-empty string`);
  }
}

function validateScoreEvent(scoreEvent) {
  if (!scoreEvent || typeof scoreEvent !== "object" || Array.isArray(scoreEvent)) {
    throw new Error("Score event JSON must contain an object");
  }

  requireNonEmptyString(scoreEvent, "userId");
  requireNonEmptyString(scoreEvent, "modelVersion");
  requireNonEmptyString(scoreEvent, "timestamp");

  if (typeof scoreEvent.newScore !== "number" || !Number.isFinite(scoreEvent.newScore)) {
    throw new Error('Score event field "newScore" must be a finite number');
  }

  if (Number.isNaN(Date.parse(scoreEvent.timestamp))) {
    throw new Error('Score event field "timestamp" must be a valid date string');
  }

  for (const field of ["positiveFactors", "negativeFactors"]) {
    if (scoreEvent[field] !== undefined && !Array.isArray(scoreEvent[field])) {
      throw new Error(`Score event field "${field}" must be an array when provided`);
    }
  }

  return scoreEvent;
}

function loadScoreEvent(
  scoreEventFile = process.env.SCORE_EVENT_FILE,
  configuredUserSalt = process.env.USER_SALT
) {
  if (!scoreEventFile) {
    return {
      scoreEvent: getMockScoreEvent(),
      userSalt: DEFAULT_USER_SALT,
      source: "built-in mock score event",
      isCustomEvent: false
    };
  }

  if (
    typeof configuredUserSalt !== "string" ||
    configuredUserSalt.length < MIN_USER_SALT_LENGTH
  ) {
    throw new Error(
      `USER_SALT must contain at least ${MIN_USER_SALT_LENGTH} characters when SCORE_EVENT_FILE is used`
    );
  }

  const resolvedPath = path.resolve(process.cwd(), scoreEventFile);
  let fileContents;

  try {
    fileContents = fs.readFileSync(resolvedPath, "utf8");
  } catch (error) {
    throw new Error(`Unable to read score event file at ${resolvedPath}: ${error.message}`);
  }

  let scoreEvent;

  try {
    scoreEvent = JSON.parse(fileContents);
  } catch (error) {
    throw new Error(`Score event file is not valid JSON: ${error.message}`);
  }

  return {
    scoreEvent: validateScoreEvent(scoreEvent),
    userSalt: configuredUserSalt,
    source: resolvedPath,
    isCustomEvent: true
  };
}

module.exports = {
  MIN_USER_SALT_LENGTH,
  loadScoreEvent,
  validateScoreEvent
};
