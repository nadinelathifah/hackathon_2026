const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { createMerkleRoot } = require("../utils/createMerkleRoot");
const { loadScoreEvent } = require("../utils/loadScoreEvent");
const { formatScorePeriod, resolveScorePeriod } = require("../utils/scorePeriod");
const { getRegistry, sameHash } = require("./helpers");

async function main() {
  const registry = await getRegistry();
  const { scoreEvent, userSalt, source } = loadScoreEvent();
  const { userHash, scoreEventHash, modelVersionHash } = hashScoreEvent(
    scoreEvent,
    userSalt
  );
  const { merkleRoot, proof, leaf } = createMerkleRoot(scoreEventHash);
  const scorePeriod = resolveScorePeriod(scoreEvent);
  const latestRecord = await registry.latestRecordByUserHash(userHash);

  console.log(`Score event source: ${source}`);
  console.log(`Score period: ${formatScorePeriod(scorePeriod)}`);
  console.log(`userHash: ${userHash}`);

  const proofValid = await registry.verifyScoreEvent(
    proof,
    latestRecord.merkleRoot,
    leaf
  );
  const valuesMatch =
    sameHash(latestRecord.scoreEventHash, scoreEventHash) &&
    sameHash(latestRecord.merkleRoot, merkleRoot) &&
    sameHash(latestRecord.modelVersionHash, modelVersionHash) &&
    Number(latestRecord.scorePeriod) === scorePeriod;

  if (proofValid && valuesMatch) {
    console.log("\nVerification result: VALID");
    console.log("The off-chain score event matches the V2 on-chain audit proof.\n");
    return;
  }

  console.log("\nVerification result: INVALID");
  console.log("The off-chain score event does not match the V2 on-chain audit proof.");
  console.log("Possible tampering or a mismatched score period was detected.\n");
  process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
