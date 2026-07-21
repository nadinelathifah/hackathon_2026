const { expect } = require("chai");
const { ethers } = require("hardhat");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");

describe("ScoreAuditRegistry", function () {
  let registry;
  let owner;
  let issuer;
  let outsider;

  const userHash = ethers.keccak256(ethers.toUtf8Bytes("ibex-user-001:test-salt"));
  const scoreEventHash = ethers.keccak256(ethers.toUtf8Bytes("mock-score-event"));
  const merkleRoot = scoreEventHash;
  const modelVersionHash = ethers.keccak256(ethers.toUtf8Bytes("ibex-credit-model-v1.0"));

  beforeEach(async function () {
    [owner, issuer, outsider] = await ethers.getSigners();

    const ScoreAuditRegistry = await ethers.getContractFactory("ScoreAuditRegistry");
    registry = await ScoreAuditRegistry.deploy();
    await registry.waitForDeployment();
  });

  it("sets the deployer as owner", async function () {
    expect(await registry.owner()).to.equal(owner.address);
  });

  it("approves the deployer as the first issuer", async function () {
    expect(await registry.approvedIssuers(owner.address)).to.equal(true);
  });

  it("allows the owner to add an issuer", async function () {
    await expect(registry.addIssuer(issuer.address))
      .to.emit(registry, "IssuerAdded")
      .withArgs(issuer.address);

    expect(await registry.approvedIssuers(issuer.address)).to.equal(true);
  });

  it("allows the owner to remove an issuer", async function () {
    await registry.addIssuer(issuer.address);

    await expect(registry.removeIssuer(issuer.address))
      .to.emit(registry, "IssuerRemoved")
      .withArgs(issuer.address);

    expect(await registry.approvedIssuers(issuer.address)).to.equal(false);
  });

  it("allows an approved issuer to submit a score root", async function () {
    await registry.addIssuer(issuer.address);

    await expect(
      registry.connect(issuer).submitScoreRoot(
        userHash,
        scoreEventHash,
        merkleRoot,
        modelVersionHash
      )
    ).to.not.be.reverted;
  });

  it("blocks a non-approved issuer from submitting a score root", async function () {
    await expect(
      registry.connect(outsider).submitScoreRoot(
        userHash,
        scoreEventHash,
        merkleRoot,
        modelVersionHash
      )
    ).to.be.revertedWith("Not approved issuer");
  });

  it("stores the latest score record correctly", async function () {
    await registry.submitScoreRoot(userHash, scoreEventHash, merkleRoot, modelVersionHash);

    const latestRecord = await registry.latestRecordByUserHash(userHash);

    expect(latestRecord.scoreEventHash).to.equal(scoreEventHash);
    expect(latestRecord.merkleRoot).to.equal(merkleRoot);
    expect(latestRecord.modelVersionHash).to.equal(modelVersionHash);
    expect(latestRecord.timestamp).to.be.greaterThan(0n);
    expect(latestRecord.issuer).to.equal(owner.address);
  });

  it("emits an event when a score root is submitted", async function () {
    await expect(registry.submitScoreRoot(userHash, scoreEventHash, merkleRoot, modelVersionHash))
      .to.emit(registry, "ScoreRootSubmitted")
      .withArgs(
        userHash,
        scoreEventHash,
        merkleRoot,
        modelVersionHash,
        anyValue,
        owner.address
      );
  });

  it("verifies a Merkle proof", async function () {
    expect(await registry.verifyScoreEvent([], merkleRoot, scoreEventHash)).to.equal(true);

    const wrongLeaf = ethers.keccak256(ethers.toUtf8Bytes("tampered-score-event"));
    expect(await registry.verifyScoreEvent([], merkleRoot, wrongLeaf)).to.equal(false);
  });
});
