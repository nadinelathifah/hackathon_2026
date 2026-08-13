const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { createMerkleRoot } = require("../utils/createMerkleRoot");
const { loadScoreEvent } = require("../utils/loadScoreEvent");
const { formatScorePeriod, resolveScorePeriod } = require("../utils/scorePeriod");
const {
  getNetworkLabel,
  getRegistry,
  requireEnv,
  waitForTransaction
} = require("./helpers");

async function main() {
  requireEnv("PRIVATE_KEY");

  const [signer] = await hre.ethers.getSigners();
  const registry = await getRegistry(signer);
  const signerAddress = await signer.getAddress();

  if (!(await registry.approvedIssuers(signerAddress))) {
    throw new Error(`Configured wallet is not an approved issuer: ${signerAddress}`);
  }

  if (await registry.paused()) {
    throw new Error("ScoreAuditRegistryV2 is currently paused");
  }

  const { scoreEvent, userSalt, source } = loadScoreEvent();
  const { userHash, scoreEventHash, modelVersionHash } = hashScoreEvent(
    scoreEvent,
    userSalt
  );
  const { merkleRoot } = createMerkleRoot(scoreEventHash);
  const scorePeriod = resolveScorePeriod(scoreEvent);
  const [, submissions, remaining] = await registry.issuerDailyUsage(signerAddress);

  console.log(`Score event source: ${source}`);
  console.log(`Score period: ${formatScorePeriod(scorePeriod)}`);
  console.log(`userHash: ${userHash}`);
  console.log(`scoreEventHash: ${scoreEventHash}`);
  console.log(`modelVersionHash: ${modelVersionHash}`);
  console.log(`merkleRoot: ${merkleRoot}`);
  console.log(`Issuer daily usage before submission: ${submissions} used, ${remaining} remaining`);

  const tx = await registry.submitScoreRoot(
    userHash,
    scoreEventHash,
    merkleRoot,
    modelVersionHash,
    scorePeriod
  );

  await waitForTransaction(tx, `Score proof anchored on ${getNetworkLabel()}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
