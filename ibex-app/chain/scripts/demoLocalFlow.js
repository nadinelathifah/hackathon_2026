const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { createMerkleRoot } = require("../utils/createMerkleRoot");
const { loadScoreEvent } = require("../utils/loadScoreEvent");
const { formatScorePeriod, resolveScorePeriod } = require("../utils/scorePeriod");
const { sameHash } = require("./helpers");

async function main() {
  console.log("\nIbex ScoreAuditRegistryV2 Demo Started\n");

  const [issuer] = await hre.ethers.getSigners();
  const ScoreAuditRegistryV2 = await hre.ethers.getContractFactory(
    "ScoreAuditRegistryV2"
  );
  const registry = await ScoreAuditRegistryV2.deploy(100);
  await registry.waitForDeployment();

  const { scoreEvent, userSalt, source, isCustomEvent } = loadScoreEvent();
  const { scoreEventHash, userHash, modelVersionHash } = hashScoreEvent(
    scoreEvent,
    userSalt
  );
  const { merkleRoot, proof, leaf } = createMerkleRoot(scoreEventHash);
  const scorePeriod = resolveScorePeriod(scoreEvent);

  console.log(
    isCustomEvent
      ? `1. Score event loaded off-chain from: ${source}`
      : "1. Mock score event created off-chain"
  );
  console.log(`2. userHash generated: ${userHash}`);
  console.log(`3. scoreEventHash generated: ${scoreEventHash}`);
  console.log(`4. modelVersionHash generated: ${modelVersionHash}`);
  console.log(`5. Merkle root generated: ${merkleRoot}`);
  console.log(`6. Score period: ${formatScorePeriod(scorePeriod)}`);

  const tx = await registry.submitScoreRoot(
    userHash,
    scoreEventHash,
    merkleRoot,
    modelVersionHash,
    scorePeriod
  );
  await tx.wait();
  console.log("7. Score proof submitted by an approved issuer");

  const latestRecord = await registry.latestRecordByUserHash(userHash);
  const proofValid = await registry.verifyScoreEvent(
    proof,
    latestRecord.merkleRoot,
    leaf
  );
  const valuesMatch =
    sameHash(latestRecord.scoreEventHash, scoreEventHash) &&
    sameHash(latestRecord.merkleRoot, merkleRoot) &&
    sameHash(latestRecord.modelVersionHash, modelVersionHash) &&
    Number(latestRecord.scorePeriod) === scorePeriod &&
    latestRecord.issuer.toLowerCase() === issuer.address.toLowerCase();

  console.log("8. Latest record read from the contract");
  console.log(`9. Verification result: ${proofValid && valuesMatch ? "VALID" : "INVALID"}`);

  if (!proofValid || !valuesMatch) {
    throw new Error("Local V2 verification failed");
  }

  let duplicateRejected = false;

  try {
    await registry.submitScoreRoot(
      userHash,
      scoreEventHash,
      merkleRoot,
      modelVersionHash,
      scorePeriod
    );
  } catch (error) {
    duplicateRejected = true;
  }

  if (!duplicateRejected) {
    throw new Error("Duplicate protection did not reject the repeated score event");
  }

  const [, submissions, remaining] = await registry.issuerDailyUsage(issuer.address);
  console.log("10. Duplicate submission rejected by V2 protections");
  console.log(`11. Issuer daily usage: ${submissions} used, ${remaining} remaining`);
  console.log("\nNo score, identity, bank transaction, income, visa, address, or ML feature data was stored on-chain.");
  console.log("Only pseudonymous proof data, operational limits, timestamp, and issuer address were stored.\n");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
