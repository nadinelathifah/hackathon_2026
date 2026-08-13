function parseScorePeriod(value) {
  if (!/^\d{6}$/.test(String(value))) {
    throw new Error("SCORE_PERIOD must use YYYYMM format, for example 202608");
  }

  const scorePeriod = Number(value);
  const year = Math.floor(scorePeriod / 100);
  const month = scorePeriod % 100;

  if (year < 2020 || month < 1 || month > 12) {
    throw new Error("SCORE_PERIOD must contain a valid year and month");
  }

  return scorePeriod;
}

function scorePeriodFromTimestamp(timestamp) {
  const date = new Date(timestamp);

  if (Number.isNaN(date.getTime())) {
    throw new Error("The score event timestamp cannot be converted into a score period");
  }

  return date.getUTCFullYear() * 100 + date.getUTCMonth() + 1;
}

function resolveScorePeriod(scoreEvent, configuredPeriod = process.env.SCORE_PERIOD) {
  if (configuredPeriod) {
    return parseScorePeriod(configuredPeriod);
  }

  return scorePeriodFromTimestamp(scoreEvent.timestamp);
}

function formatScorePeriod(scorePeriod) {
  const value = String(scorePeriod);
  return `${value.slice(0, 4)}-${value.slice(4)}`;
}

module.exports = {
  formatScorePeriod,
  parseScorePeriod,
  resolveScorePeriod,
  scorePeriodFromTimestamp
};
