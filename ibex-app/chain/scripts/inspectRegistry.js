const hre = require("hardhat");
const { getContractAddress, getRegistry } = require("./helpers");

async function main() {
  const contractAddress = getContractAddress();
  const registry = await getRegistry();
  const network = await hre.ethers.provider.getNetwork();
  const deployedCode = await hre.ethers.provider.getCode(contractAddress);
  const artifact = await hre.artifacts.readArtifact("ScoreAuditRegistryV2");
  const owner = await registry.owner();
  const pendingOwner = await registry.pendingOwner();

  console.log(`Network: ${hre.network.name}`);
  console.log(`Chain ID: ${network.chainId}`);
  console.log(`Contract: ${contractAddress}`);
  console.log(`Contract bytecode present: ${deployedCode !== "0x"}`);
  console.log(
    `Bytecode matches local V2 artifact: ${
      deployedCode.toLowerCase() === artifact.deployedBytecode.toLowerCase()
    }`
  );
  console.log(`Owner: ${owner}`);
  console.log(`Pending owner: ${pendingOwner}`);
  console.log(`Owner is approved issuer: ${await registry.approvedIssuers(owner)}`);
  console.log(`Paused: ${await registry.paused()}`);
  console.log(`Daily issuer submission limit: ${await registry.dailyIssuerSubmissionLimit()}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
