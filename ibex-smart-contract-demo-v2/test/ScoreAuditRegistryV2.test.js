const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");

describe("ScoreAuditRegistryV2", function () {
  let registry;
  let owner;
  let issuer;
  let outsider;

  const dailyLimit = 3n;
  const scorePeriod = 202608;
  const userHash = ethers.keccak256(ethers.toUtf8Bytes("ibex-user-001:test-salt"));
  const scoreEventHash = ethers.keccak256(ethers.toUtf8Bytes("mock-score-event-202608"));
  const merkleRoot = scoreEventHash;
  const modelVersionHash = ethers.keccak256(
    ethers.toUtf8Bytes("ibex-credit-model-v2.0")
  );

  function hash(label) {
    return ethers.keccak256(ethers.toUtf8Bytes(label));
  }

  async function deployRegistry(limit = dailyLimit) {
    const ScoreAuditRegistryV2 = await ethers.getContractFactory(
      "ScoreAuditRegistryV2"
    );
    const deployedRegistry = await ScoreAuditRegistryV2.deploy(limit);
    await deployedRegistry.waitForDeployment();
    return deployedRegistry;
  }

  async function submit({
    signer = owner,
    submittedUserHash = userHash,
    submittedScoreEventHash = scoreEventHash,
    submittedMerkleRoot = merkleRoot,
    submittedModelVersionHash = modelVersionHash,
    submittedScorePeriod = scorePeriod
  } = {}) {
    return registry.connect(signer).submitScoreRoot(
      submittedUserHash,
      submittedScoreEventHash,
      submittedMerkleRoot,
      submittedModelVersionHash,
      submittedScorePeriod
    );
  }

  beforeEach(async function () {
    [owner, issuer, outsider] = await ethers.getSigners();
    registry = await deployRegistry();
  });

  describe("deployment and issuer administration", function () {
    it("sets the deployer as owner and first approved issuer", async function () {
      expect(await registry.owner()).to.equal(owner.address);
      expect(await registry.approvedIssuers(owner.address)).to.equal(true);
      expect(await registry.dailyIssuerSubmissionLimit()).to.equal(dailyLimit);
    });

    it("rejects an invalid initial daily limit", async function () {
      const ScoreAuditRegistryV2 = await ethers.getContractFactory(
        "ScoreAuditRegistryV2"
      );

      await expect(ScoreAuditRegistryV2.deploy(0))
        .to.be.revertedWithCustomError(
          ScoreAuditRegistryV2,
          "InvalidDailyIssuerSubmissionLimit"
        )
        .withArgs(0);
    });

    it("allows only the owner to add and remove issuers", async function () {
      await expect(registry.addIssuer(issuer.address))
        .to.emit(registry, "IssuerAdded")
        .withArgs(issuer.address);

      expect(await registry.approvedIssuers(issuer.address)).to.equal(true);

      await expect(registry.connect(outsider).removeIssuer(issuer.address))
        .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
        .withArgs(outsider.address);

      await expect(registry.removeIssuer(issuer.address))
        .to.emit(registry, "IssuerRemoved")
        .withArgs(issuer.address);

      expect(await registry.approvedIssuers(issuer.address)).to.equal(false);
    });

    it("rejects zero, duplicate, and already-removed issuers", async function () {
      await expect(registry.addIssuer(ethers.ZeroAddress))
        .to.be.revertedWithCustomError(registry, "InvalidIssuer")
        .withArgs(ethers.ZeroAddress);

      await expect(registry.addIssuer(owner.address))
        .to.be.revertedWithCustomError(registry, "IssuerAlreadyApproved")
        .withArgs(owner.address);

      await expect(registry.removeIssuer(issuer.address))
        .to.be.revertedWithCustomError(registry, "IssuerNotApproved")
        .withArgs(issuer.address);
    });

    it("blocks non-approved and removed issuers from submitting", async function () {
      await expect(submit({ signer: outsider }))
        .to.be.revertedWithCustomError(registry, "NotApprovedIssuer")
        .withArgs(outsider.address);

      await registry.addIssuer(issuer.address);
      await registry.removeIssuer(issuer.address);

      await expect(submit({ signer: issuer }))
        .to.be.revertedWithCustomError(registry, "NotApprovedIssuer")
        .withArgs(issuer.address);
    });
  });

  describe("score proof submission", function () {
    it("stores the latest proof and emits its audit event", async function () {
      await expect(submit())
        .to.emit(registry, "ScoreRootSubmitted")
        .withArgs(
          userHash,
          scoreEventHash,
          merkleRoot,
          modelVersionHash,
          scorePeriod,
          anyValue,
          owner.address
        );

      const latestRecord = await registry.latestRecordByUserHash(userHash);

      expect(latestRecord.scoreEventHash).to.equal(scoreEventHash);
      expect(latestRecord.merkleRoot).to.equal(merkleRoot);
      expect(latestRecord.modelVersionHash).to.equal(modelVersionHash);
      expect(latestRecord.timestamp).to.be.greaterThan(0n);
      expect(latestRecord.scorePeriod).to.equal(scorePeriod);
      expect(latestRecord.issuer).to.equal(owner.address);
      expect(await registry.usedScoreEventHashes(scoreEventHash)).to.equal(true);
    });

    const zeroHashCases = [
      ["userHash", { submittedUserHash: ethers.ZeroHash }],
      ["scoreEventHash", { submittedScoreEventHash: ethers.ZeroHash }],
      ["merkleRoot", { submittedMerkleRoot: ethers.ZeroHash }],
      ["modelVersionHash", { submittedModelVersionHash: ethers.ZeroHash }]
    ];

    for (const [field, overrides] of zeroHashCases) {
      it(`rejects a zero ${field}`, async function () {
        await expect(submit(overrides))
          .to.be.revertedWithCustomError(registry, "InvalidHash")
          .withArgs(field);
      });
    }

    it("rejects malformed score periods", async function () {
      for (const invalidPeriod of [201912, 202600, 202613]) {
        await expect(submit({ submittedScorePeriod: invalidPeriod }))
          .to.be.revertedWithCustomError(registry, "InvalidScorePeriod")
          .withArgs(invalidPeriod);
      }
    });

    it("rejects the same or an older score period", async function () {
      await submit();
      await time.increase(28 * 24 * 60 * 60);

      await expect(
        submit({
          submittedScoreEventHash: hash("different-event-same-period"),
          submittedMerkleRoot: hash("different-event-same-period")
        })
      )
        .to.be.revertedWithCustomError(registry, "ScorePeriodNotNewer")
        .withArgs(scorePeriod, scorePeriod);

      await expect(
        submit({
          submittedScoreEventHash: hash("different-event-older-period"),
          submittedMerkleRoot: hash("different-event-older-period"),
          submittedScorePeriod: 202607
        })
      )
        .to.be.revertedWithCustomError(registry, "ScorePeriodNotNewer")
        .withArgs(202607, scorePeriod);
    });

    it("rejects a newer period during the 28-day cooldown", async function () {
      await submit();

      await expect(
        submit({
          submittedScoreEventHash: hash("mock-score-event-202609"),
          submittedMerkleRoot: hash("mock-score-event-202609"),
          submittedScorePeriod: 202609
        })
      )
        .to.be.revertedWithCustomError(registry, "ScoreUpdateTooSoon")
        .withArgs(anyValue);
    });

    it("allows a newer score after the cooldown", async function () {
      await submit();
      const nextSubmissionAt = await registry.nextSubmissionAt(userHash);
      await time.increaseTo(nextSubmissionAt);

      const nextEventHash = hash("mock-score-event-202609");
      await submit({
        submittedScoreEventHash: nextEventHash,
        submittedMerkleRoot: nextEventHash,
        submittedScorePeriod: 202609
      });

      const latestRecord = await registry.latestRecordByUserHash(userHash);
      expect(latestRecord.scoreEventHash).to.equal(nextEventHash);
      expect(latestRecord.scorePeriod).to.equal(202609);
    });

    it("rejects a score event hash already used for another user", async function () {
      await submit();

      await expect(
        submit({ submittedUserHash: hash("another-user") })
      )
        .to.be.revertedWithCustomError(registry, "ScoreEventHashAlreadyUsed")
        .withArgs(scoreEventHash);
    });

    it("verifies valid and invalid Merkle proofs", async function () {
      expect(await registry.verifyScoreEvent([], merkleRoot, scoreEventHash)).to.equal(
        true
      );
      expect(
        await registry.verifyScoreEvent([], merkleRoot, hash("tampered-score-event"))
      ).to.equal(false);
    });
  });

  describe("abuse controls", function () {
    it("enforces and reports the daily issuer submission limit", async function () {
      registry = await deployRegistry(2);

      await submit();
      await submit({
        submittedUserHash: hash("user-2"),
        submittedScoreEventHash: hash("event-2"),
        submittedMerkleRoot: hash("event-2")
      });

      const [, submissions, remaining] = await registry.issuerDailyUsage(owner.address);
      expect(submissions).to.equal(2);
      expect(remaining).to.equal(0);

      await expect(
        submit({
          submittedUserHash: hash("user-3"),
          submittedScoreEventHash: hash("event-3"),
          submittedMerkleRoot: hash("event-3")
        })
      )
        .to.be.revertedWithCustomError(
          registry,
          "DailyIssuerSubmissionLimitReached"
        )
        .withArgs(owner.address, 2);
    });

    it("resets issuer usage on the next UTC day", async function () {
      registry = await deployRegistry(1);
      await submit();

      const [currentDay] = await registry.issuerDailyUsage(owner.address);
      await time.increaseTo((currentDay + 1n) * 24n * 60n * 60n);

      await expect(
        submit({
          submittedUserHash: hash("next-day-user"),
          submittedScoreEventHash: hash("next-day-event"),
          submittedMerkleRoot: hash("next-day-event")
        })
      ).to.not.be.reverted;
    });

    it("allows the owner to update the daily limit", async function () {
      await expect(registry.setDailyIssuerSubmissionLimit(25))
        .to.emit(registry, "DailyIssuerSubmissionLimitUpdated")
        .withArgs(dailyLimit, 25);

      expect(await registry.dailyIssuerSubmissionLimit()).to.equal(25);

      await expect(registry.connect(outsider).setDailyIssuerSubmissionLimit(10))
        .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
        .withArgs(outsider.address);
    });

    it("reports zero remaining when the limit is lowered below today's usage", async function () {
      await submit();
      await submit({
        submittedUserHash: hash("second-user"),
        submittedScoreEventHash: hash("second-event"),
        submittedMerkleRoot: hash("second-event")
      });

      await registry.setDailyIssuerSubmissionLimit(1);

      const [, submissions, remaining] = await registry.issuerDailyUsage(owner.address);
      expect(submissions).to.equal(2);
      expect(remaining).to.equal(0);
    });

    it("allows the owner to pause and unpause submissions", async function () {
      await expect(registry.pause()).to.emit(registry, "Paused").withArgs(owner.address);

      await expect(submit()).to.be.revertedWithCustomError(registry, "EnforcedPause");

      await expect(registry.connect(outsider).unpause())
        .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
        .withArgs(outsider.address);

      await expect(registry.unpause())
        .to.emit(registry, "Unpaused")
        .withArgs(owner.address);

      await expect(submit()).to.not.be.reverted;
    });
  });

  describe("ownership safety", function () {
    it("requires the pending owner to accept ownership", async function () {
      await registry.transferOwnership(issuer.address);

      expect(await registry.owner()).to.equal(owner.address);
      expect(await registry.pendingOwner()).to.equal(issuer.address);

      await expect(registry.connect(outsider).acceptOwnership())
        .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
        .withArgs(outsider.address);

      await registry.connect(issuer).acceptOwnership();

      expect(await registry.owner()).to.equal(issuer.address);
      expect(await registry.pendingOwner()).to.equal(ethers.ZeroAddress);
    });

    it("disables accidental ownership renunciation", async function () {
      await expect(registry.renounceOwnership()).to.be.revertedWithCustomError(
        registry,
        "OwnershipRenunciationDisabled"
      );
    });
  });
});
