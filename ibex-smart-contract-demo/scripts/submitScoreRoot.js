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
  const { userHash, scoreEventHash, modelVersionHash } = hashScoreEvent();
  const { merkleRoot } = createMerkleRoot(scoreEventHash);

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
