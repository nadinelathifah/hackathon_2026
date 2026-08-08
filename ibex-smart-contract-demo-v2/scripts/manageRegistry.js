const hre = require("hardhat");
const {
  getRegistry,
  parsePositiveInteger,
  requireEnv,
  waitForTransaction
} = require("./helpers");

const OWNER_ACTIONS = new Set([
  "add-issuer",
  "remove-issuer",
  "pause",
  "unpause",
  "set-daily-limit",
  "transfer-ownership"
]);

async function requireOwner(registry, signerAddress) {
  const ownerAddress = await registry.owner();

  if (signerAddress.toLowerCase() !== ownerAddress.toLowerCase()) {
    throw new Error(
      `Configured wallet ${signerAddress} is not the contract owner ${ownerAddress}`
    );
  }
}

async function main() {
  requireEnv("PRIVATE_KEY");

  const action = requireEnv("V2_ADMIN_ACTION").toLowerCase();
  const [signer] = await hre.ethers.getSigners();
  const signerAddress = await signer.getAddress();
  const registry = await getRegistry(signer);

  if (OWNER_ACTIONS.has(action)) {
    await requireOwner(registry, signerAddress);
  }

  if (action === "add-issuer") {
    const issuerAddress = hre.ethers.getAddress(requireEnv("ISSUER_ADDRESS"));

    if (await registry.approvedIssuers(issuerAddress)) {
      console.log(`Issuer is already approved: ${issuerAddress}`);
      return;
    }

    await waitForTransaction(
      await registry.addIssuer(issuerAddress),
      `Approved V2 issuer: ${issuerAddress}`
    );
    return;
  }

  if (action === "remove-issuer") {
    const issuerAddress = hre.ethers.getAddress(requireEnv("ISSUER_ADDRESS"));

    if (!(await registry.approvedIssuers(issuerAddress))) {
      console.log(`Issuer is already removed: ${issuerAddress}`);
      return;
    }

    await waitForTransaction(
      await registry.removeIssuer(issuerAddress),
      `Removed V2 issuer: ${issuerAddress}`
    );
    return;
  }

  if (action === "pause") {
    if (await registry.paused()) {
      console.log("ScoreAuditRegistryV2 is already paused.");
      return;
    }

    await waitForTransaction(await registry.pause(), "ScoreAuditRegistryV2 paused");
    return;
  }

  if (action === "unpause") {
    if (!(await registry.paused())) {
      console.log("ScoreAuditRegistryV2 is already active.");
      return;
    }

    await waitForTransaction(await registry.unpause(), "ScoreAuditRegistryV2 unpaused");
    return;
  }

  if (action === "set-daily-limit") {
    const newLimit = parsePositiveInteger(
      requireEnv("V2_DAILY_ISSUER_LIMIT"),
      "V2_DAILY_ISSUER_LIMIT"
    );

    if ((await registry.dailyIssuerSubmissionLimit()) === newLimit) {
      console.log(`Daily V2 submission limit is already: ${newLimit}`);
      return;
    }

    await waitForTransaction(
      await registry.setDailyIssuerSubmissionLimit(newLimit),
      `Daily V2 submission limit set to: ${newLimit}`
    );
    return;
  }

  if (action === "transfer-ownership") {
    const newOwnerAddress = hre.ethers.getAddress(requireEnv("NEW_OWNER_ADDRESS"));

    await waitForTransaction(
      await registry.transferOwnership(newOwnerAddress),
      `Ownership transfer started for: ${newOwnerAddress}`
    );
    console.log("The new owner must now run the accept-ownership action from their wallet.");
    return;
  }

  if (action === "accept-ownership") {
    const pendingOwner = await registry.pendingOwner();

    if (signerAddress.toLowerCase() !== pendingOwner.toLowerCase()) {
      throw new Error(
        `Configured wallet ${signerAddress} is not the pending owner ${pendingOwner}`
      );
    }

    await waitForTransaction(
      await registry.acceptOwnership(),
      `V2 ownership accepted by: ${signerAddress}`
    );
    return;
  }

  throw new Error(
    "Unknown V2_ADMIN_ACTION. Use add-issuer, remove-issuer, pause, unpause, set-daily-limit, transfer-ownership, or accept-ownership."
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
