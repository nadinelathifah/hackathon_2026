const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { createMerkleRoot } = require("../utils/createMerkleRoot");

function requireEnv(name) {
  const value = process.env[name];

  if (!value) {
    throw new Error(`${name} is required in .env`);
  }

  return value;
}

function sameHash(left, right) {
  return left.toLowerCase() === right.toLowerCase();
}

async function main() {
  const contractAddress = requireEnv("SCORE_AUDIT_CONTRACT_ADDRESS");
  const registry = await hre.ethers.getContractAt("ScoreAuditRegistry", contractAddress);

  const { userHash, scoreEventHash, modelVersionHash } = hashScoreEvent();
  const { merkleRoot, proof, leaf } = createMerkleRoot(scoreEventHash);
  const latestRecord = await registry.latestRecordByUserHash(userHash);

  const proofValid = await registry.verifyScoreEvent(proof, latestRecord.merkleRoot, leaf);
  const valuesMatch =
    sameHash(latestRecord.scoreEventHash, scoreEventHash) &&
    sameHash(latestRecord.merkleRoot, merkleRoot) &&
    sameHash(latestRecord.modelVersionHash, modelVersionHash);

  if (proofValid && valuesMatch) {
    console.log("\nVerification result: VALID");
    console.log("The off-chain score event matches the on-chain audit proof.\n");
    return;
  }

  console.log("\nVerification result: INVALID");
  console.log("The off-chain score event does not match the on-chain audit proof.");
  console.log("Possible tampering detected.\n");
  process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
