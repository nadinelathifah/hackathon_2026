const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { loadScoreEvent } = require("../utils/loadScoreEvent");
const { formatScorePeriod } = require("../utils/scorePeriod");
const { formatTimestamp, getRegistry } = require("./helpers");

async function main() {
  const registry = await getRegistry();
  let userHash = process.env.USER_HASH;

  if (!userHash) {
    const { scoreEvent, userSalt } = loadScoreEvent();
    userHash = hashScoreEvent(scoreEvent, userSalt).userHash;
  }

  const latestRecord = await registry.latestRecordByUserHash(userHash);

  console.log("\nLatest V2 score audit record:");
  console.log(`userHash: ${userHash}`);

  if (latestRecord.timestamp === 0n) {
    console.log("No score audit record exists for this userHash.\n");
    return;
  }

  const nextSubmissionAt = await registry.nextSubmissionAt(userHash);

  console.log(`scoreEventHash: ${latestRecord.scoreEventHash}`);
  console.log(`merkleRoot: ${latestRecord.merkleRoot}`);
  console.log(`modelVersionHash: ${latestRecord.modelVersionHash}`);
  console.log(`scorePeriod: ${formatScorePeriod(Number(latestRecord.scorePeriod))}`);
  console.log(`timestamp: ${formatTimestamp(latestRecord.timestamp)}`);
  console.log(`issuer: ${latestRecord.issuer}`);
  console.log(`next contract-allowed update: ${formatTimestamp(nextSubmissionAt)}\n`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
