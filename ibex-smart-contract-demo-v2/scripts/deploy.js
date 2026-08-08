const hre = require("hardhat");
const { parsePositiveInteger } = require("./helpers");

async function main() {
  const dailyLimit = parsePositiveInteger(
    process.env.V2_DAILY_ISSUER_LIMIT || "1000",
    "V2_DAILY_ISSUER_LIMIT"
  );

  const ScoreAuditRegistryV2 = await hre.ethers.getContractFactory(
    "ScoreAuditRegistryV2"
  );
  const registry = await ScoreAuditRegistryV2.deploy(dailyLimit);

  await registry.waitForDeployment();

  console.log(`ScoreAuditRegistryV2 deployed to: ${await registry.getAddress()}`);
  console.log(`Daily submissions allowed per issuer: ${dailyLimit}`);
  console.log("The deployer is the owner and first approved issuer.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
