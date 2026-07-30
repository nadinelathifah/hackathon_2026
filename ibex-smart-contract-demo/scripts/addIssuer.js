const hre = require("hardhat");

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

async function main() {
  requireEnv("PRIVATE_KEY");

  const contractAddress = requireEnv("SCORE_AUDIT_CONTRACT_ADDRESS");
  const issuerAddress = hre.ethers.getAddress(requireEnv("ISSUER_ADDRESS"));

  if (issuerAddress === hre.ethers.ZeroAddress) {
    throw new Error("ISSUER_ADDRESS cannot be the zero address");
  }

  const [signer] = await hre.ethers.getSigners();
  const signerAddress = await signer.getAddress();
  const registry = await hre.ethers.getContractAt("ScoreAuditRegistry", contractAddress, signer);
  const ownerAddress = await registry.owner();

  if (signerAddress.toLowerCase() !== ownerAddress.toLowerCase()) {
    throw new Error(
      `Configured wallet ${signerAddress} is not the contract owner ${ownerAddress}`
    );
  }

  if (await registry.approvedIssuers(issuerAddress)) {
    console.log(`Issuer is already approved: ${issuerAddress}`);
    return;
  }

  const tx = await registry.addIssuer(issuerAddress);
  console.log(`Transaction sent: ${tx.hash}`);

  await tx.wait();

  const explorerBaseUrl = getExplorerBaseUrl(hre.network.name);
  console.log(`Approved issuer: ${issuerAddress}`);
  console.log(`PolygonScan link: ${explorerBaseUrl}/tx/${tx.hash}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
