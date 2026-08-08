const hre = require("hardhat");

function requireEnv(name) {
  const value = process.env[name];

  if (!value) {
    throw new Error(`${name} is required in .env`);
  }

  return value.trim();
}

function getContractAddress() {
  return requireEnv("SCORE_AUDIT_V2_CONTRACT_ADDRESS");
}

function getExplorerBaseUrl(networkName = hre.network.name) {
  if (networkName === "amoy") {
    return process.env.AMOY_EXPLORER_BASE_URL || "https://amoy.polygonscan.com";
  }

  return process.env.POLYGON_EXPLORER_BASE_URL || "https://polygonscan.com";
}

function getNetworkLabel(networkName = hre.network.name) {
  if (networkName === "amoy") {
    return "Polygon Amoy";
  }

  if (networkName === "polygon") {
    return "Polygon mainnet";
  }

  return networkName;
}

function parsePositiveInteger(value, name) {
  if (!/^\d+$/.test(String(value)) || BigInt(value) <= 0n) {
    throw new Error(`${name} must be a positive whole number`);
  }

  return BigInt(value);
}

function formatTimestamp(timestamp) {
  if (timestamp === 0n) {
    return "0";
  }

  return `${timestamp.toString()} (${new Date(Number(timestamp) * 1000).toISOString()})`;
}

function sameHash(left, right) {
  return left.toLowerCase() === right.toLowerCase();
}

async function getRegistry(signer) {
  return hre.ethers.getContractAt(
    "ScoreAuditRegistryV2",
    getContractAddress(),
    signer
  );
}

async function waitForTransaction(tx, description) {
  console.log(`Transaction sent: ${tx.hash}`);
  await tx.wait();
  console.log(description);
  console.log(`PolygonScan link: ${getExplorerBaseUrl()}/tx/${tx.hash}`);
}

module.exports = {
  formatTimestamp,
  getContractAddress,
  getExplorerBaseUrl,
  getNetworkLabel,
  getRegistry,
  parsePositiveInteger,
  requireEnv,
  sameHash,
  waitForTransaction
};
