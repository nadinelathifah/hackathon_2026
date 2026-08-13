const hre = require("hardhat");
const { parsePositiveInteger } = require("./helpers");

async function main() {
  const dailyLimit = parsePositiveInteger(
    process.env.V2_DAILY_ISSUER_LIMIT || "1000",
    "V2_DAILY_ISSUER_LIMIT"
  );
  const [signer] = await hre.ethers.getSigners();
  const factory = await hre.ethers.getContractFactory("ScoreAuditRegistryV2", signer);
  const deploymentTransaction = await factory.getDeployTransaction(dailyLimit);
  const estimatedGas = await hre.ethers.provider.estimateGas({
    ...deploymentTransaction,
    from: signer.address
  });
  const feeData = await hre.ethers.provider.getFeeData();
  const pricePerGas = feeData.maxFeePerGas || feeData.gasPrice;

  if (!pricePerGas) {
    throw new Error("The Polygon RPC did not return a usable gas price");
  }

  const estimatedCost = estimatedGas * pricePerGas;
  const bufferedGas = (estimatedGas * 120n) / 100n;
  const bufferedCost = bufferedGas * pricePerGas;

  console.log(`Network: ${hre.network.name}`);
  console.log(`Deployment wallet: ${signer.address}`);
  console.log(`Daily issuer limit: ${dailyLimit}`);
  console.log(`Estimated deployment gas: ${estimatedGas}`);
  console.log(`Estimated maximum fee per gas: ${hre.ethers.formatUnits(pricePerGas, "gwei")} gwei`);
  console.log(`Estimated deployment cost: ${hre.ethers.formatEther(estimatedCost)} POL`);
  console.log(`20% buffered estimate: ${hre.ethers.formatEther(bufferedCost)} POL`);
  console.log("No transaction was sent.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
