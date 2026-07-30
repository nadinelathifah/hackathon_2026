const hre = require("hardhat");
const { hashScoreEvent } = require("../utils/hashScoreEvent");
const { createMerkleRoot } = require("../utils/createMerkleRoot");
const { loadScoreEvent } = require("../utils/loadScoreEvent");

function requireEnv(name) {
  const value = process.env[name];

  if (!value) {
    throw new Error(`${name} is required in .env`);
  }

  return value;
}

function getExplorerBaseUrl(networkName) {
  if (networkName === "amoy") {
    return process.env.AMOY_EXPLORER_BASE_URL || "https://amoy.polygonscan.com";
  }

  return process.env.POLYGON_EXPLORER_BASE_URL || "https://polygonscan.com";
}

function getNetworkLabel(networkName) {
  if (networkName === "amoy") {
    return "Polygon Amoy";
  }

  if (networkName === "polygon") {
    return "Polygon mainnet";
  }

  return networkName;
}

async function main() {
  requireEnv("PRIVATE_KEY");

  const contractAddress = requireEnv("SCORE_AUDIT_CONTRACT_ADDRESS");
  const explorerBaseUrl = getExplorerBaseUrl(hre.network.name);
  const networkLabel = getNetworkLabel(hre.network.name);

  const registry = await hre.ethers.getContractAt("ScoreAuditRegistry", contractAddress);
  const { scoreEvent, userSalt, source } = loadScoreEvent();
  const { userHash, scoreEventHash, modelVersionHash } = hashScoreEvent(
    scoreEvent,
    userSalt
  );
  const { merkleRoot } = createMerkleRoot(scoreEventHash);

  console.log(`Score event source: ${source}`);
  console.log(`userHash: ${userHash}`);
  console.log(`scoreEventHash: ${scoreEventHash}`);
  console.log(`modelVersionHash: ${modelVersionHash}`);
  console.log(`merkleRoot: ${merkleRoot}`);

  const tx = await registry.submitScoreRoot(
    userHash,
    scoreEventHash,
    merkleRoot,
    modelVersionHash
  );

  console.log(`Transaction sent: ${tx.hash}`);

  await tx.wait();

  console.log(`Score proof anchored on ${networkLabel}`);
  console.log(`PolygonScan link: ${explorerBaseUrl}/tx/${tx.hash}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
