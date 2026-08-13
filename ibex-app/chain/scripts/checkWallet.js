const hre = require("hardhat");

async function main() {
  const [signer] = await hre.ethers.getSigners();

  if (!signer) {
    throw new Error("No signer found. Add PRIVATE_KEY to .env first.");
  }

  const providerNetwork = await hre.ethers.provider.getNetwork();
  const balance = await hre.ethers.provider.getBalance(signer.address);

  console.log(`Network: ${hre.network.name}`);
  console.log(`Chain ID: ${providerNetwork.chainId.toString()}`);
  console.log(`Wallet address: ${signer.address}`);
  console.log(`Balance: ${hre.ethers.formatEther(balance)} POL`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
