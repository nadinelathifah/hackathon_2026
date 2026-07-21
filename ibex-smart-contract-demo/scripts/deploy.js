const hre = require("hardhat");

async function main() {
  const ScoreAuditRegistry = await hre.ethers.getContractFactory("ScoreAuditRegistry");
  const registry = await ScoreAuditRegistry.deploy();

  await registry.waitForDeployment();

  console.log(`ScoreAuditRegistry deployed to: ${await registry.getAddress()}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
