const hre = require("hardhat");
const {
  getExplorerBaseUrl,
  parsePositiveInteger
} = require("./helpers");

async function main() {
  const dailyLimit = parsePositiveInteger(
    process.env.V2_DAILY_ISSUER_LIMIT || "1000",
    "V2_DAILY_ISSUER_LIMIT"
  );

  const ScoreAuditRegistryV2 = await hre.ethers.getContractFactory(
    "ScoreAuditRegistryV2"
  );
  const registry = await ScoreAuditRegistryV2.deploy(dailyLimit);
  const deploymentTransaction = registry.deploymentTransaction();

  console.log(`Transaction sent: ${deploymentTransaction.hash}`);

  await registry.waitForDeployment();

  const receipt = await deploymentTransaction.wait();
  const actualFee = receipt.fee || receipt.gasUsed * receipt.gasPrice;
  const contractAddress = await registry.getAddress();

  console.log(`ScoreAuditRegistryV2 deployed to: ${contractAddress}`);
  console.log(`Daily submissions allowed per issuer: ${dailyLimit}`);
  console.log("The deployer is the owner and first approved issuer.");
  console.log(`Gas used: ${receipt.gasUsed}`);
  console.log(`Actual deployment fee: ${hre.ethers.formatEther(actualFee)} POL`);
  console.log(`PolygonScan transaction: ${getExplorerBaseUrl()}/tx/${receipt.hash}`);
  console.log(`PolygonScan contract: ${getExplorerBaseUrl()}/address/${contractAddress}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
