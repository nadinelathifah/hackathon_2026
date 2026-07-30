const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { createMerkleRoot } = require("../utils/createMerkleRoot");
const { loadScoreEvent } = require("../utils/loadScoreEvent");

function sameHash(left, right) {
  return left.toLowerCase() === right.toLowerCase();
}

async function main() {
  console.log("\nIbex Smart Contract Demo Started\n");

  const [issuer] = await hre.ethers.getSigners();
  const ScoreAuditRegistry = await hre.ethers.getContractFactory("ScoreAuditRegistry");
  const registry = await ScoreAuditRegistry.deploy();

  await registry.waitForDeployment();

  const { scoreEvent, userSalt, source, isCustomEvent } = loadScoreEvent();
  const {
    scoreEventHash,
    userHash,
    modelVersionHash
  } = hashScoreEvent(scoreEvent, userSalt);

  const { merkleRoot, proof, leaf } = createMerkleRoot(scoreEventHash);

  console.log(
    isCustomEvent
      ? `1. Score event loaded off-chain from: ${source}`
      : "1. Mock score event created off-chain"
  );
  console.log(`2. userHash generated: ${userHash}`);
  console.log(`3. scoreEventHash generated: ${scoreEventHash}`);
  console.log(`4. modelVersionHash generated: ${modelVersionHash}`);
  console.log(`5. Merkle root generated: ${merkleRoot}`);

  const tx = await registry.submitScoreRoot(
    userHash,
    scoreEventHash,
    merkleRoot,
    modelVersionHash
  );
  await tx.wait();

  console.log("6. Score proof submitted to smart contract");

  const latestRecord = await registry.latestRecordByUserHash(userHash);
  console.log("7. Latest record read from smart contract");

  const merkleProofValid = await registry.verifyScoreEvent(proof, latestRecord.merkleRoot, leaf);
  const hashesMatch =
    sameHash(latestRecord.scoreEventHash, scoreEventHash) &&
    sameHash(latestRecord.merkleRoot, merkleRoot) &&
    sameHash(latestRecord.modelVersionHash, modelVersionHash) &&
    latestRecord.issuer.toLowerCase() === issuer.address.toLowerCase();

  const isValid = merkleProofValid && hashesMatch;
  console.log(`8. Verification result: ${isValid ? "VALID" : "INVALID"}`);

  if (!isValid) {
    throw new Error("Local verification failed");
  }

  console.log("\nNo personal data was stored on-chain.");
  console.log("Only hashes, Merkle root, timestamp, and issuer address were stored.\n");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
