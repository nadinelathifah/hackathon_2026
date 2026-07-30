const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { loadScoreEvent } = require("../utils/loadScoreEvent");

function requireEnv(name) {
  const value = process.env[name];

  if (!value) {
    throw new Error(`${name} is required in .env`);
  }

  return value;
}

function formatTimestamp(timestamp) {
  if (timestamp === 0n) {
    return "0";
  }

  return `${timestamp.toString()} (${new Date(Number(timestamp) * 1000).toISOString()})`;
}

async function main() {
  const contractAddress = requireEnv("SCORE_AUDIT_CONTRACT_ADDRESS");
  const registry = await hre.ethers.getContractAt("ScoreAuditRegistry", contractAddress);

  let userHash = process.env.USER_HASH;

  if (!userHash) {
    const { scoreEvent, userSalt } = loadScoreEvent();
    userHash = hashScoreEvent(scoreEvent, userSalt).userHash;
  }

  const latestRecord = await registry.latestRecordByUserHash(userHash);

  console.log("\nLatest score audit record:");
  console.log(`userHash: ${userHash}`);
  console.log(`scoreEventHash: ${latestRecord.scoreEventHash}`);
  console.log(`merkleRoot: ${latestRecord.merkleRoot}`);
  console.log(`modelVersionHash: ${latestRecord.modelVersionHash}`);
  console.log(`timestamp: ${formatTimestamp(latestRecord.timestamp)}`);
  console.log(`issuer: ${latestRecord.issuer}\n`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
