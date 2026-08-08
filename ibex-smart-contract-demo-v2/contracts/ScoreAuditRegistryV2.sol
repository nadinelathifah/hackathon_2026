// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/access/Ownable2Step.sol";
import "@openzeppelin/contracts/utils/Pausable.sol";
import "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

/// @title Ibex Credit Score Audit Registry V2
/// @notice Stores tamper-evident proofs for off-chain credit score events.
/// @dev Raw score events, identities, financial data, and ML features must remain off-chain.
contract ScoreAuditRegistryV2 is Ownable2Step, Pausable {
    uint256 public constant MIN_SCORE_UPDATE_INTERVAL = 28 days;

    mapping(address => bool) public approvedIssuers;

    struct LatestScoreRecord {
        bytes32 scoreEventHash;
        bytes32 merkleRoot;
        bytes32 modelVersionHash;
        uint64 timestamp;
        uint32 scorePeriod;
        address issuer;
    }

    struct DailyIssuerUsage {
        uint64 day;
        uint192 submissions;
    }

    mapping(bytes32 => LatestScoreRecord) public latestRecordByUserHash;
    mapping(bytes32 => bool) public usedScoreEventHashes;
    mapping(address => DailyIssuerUsage) private _dailyUsageByIssuer;

    uint256 public dailyIssuerSubmissionLimit;

    event IssuerAdded(address indexed issuer);
    event IssuerRemoved(address indexed issuer);
    event DailyIssuerSubmissionLimitUpdated(uint256 previousLimit, uint256 newLimit);

    event ScoreRootSubmitted(
        bytes32 indexed userHash,
        bytes32 indexed scoreEventHash,
        bytes32 merkleRoot,
        bytes32 modelVersionHash,
        uint32 scorePeriod,
        uint256 timestamp,
        address indexed issuer
    );

    error NotApprovedIssuer(address caller);
    error InvalidIssuer(address issuer);
    error IssuerAlreadyApproved(address issuer);
    error IssuerNotApproved(address issuer);
    error InvalidHash(string field);
    error InvalidScorePeriod(uint32 scorePeriod);
    error ScorePeriodNotNewer(uint32 submittedPeriod, uint32 latestPeriod);
    error ScoreUpdateTooSoon(uint256 nextAllowedTimestamp);
    error ScoreEventHashAlreadyUsed(bytes32 scoreEventHash);
    error InvalidDailyIssuerSubmissionLimit(uint256 limit);
    error DailyIssuerSubmissionLimitReached(address issuer, uint256 limit);
    error OwnershipRenunciationDisabled();

    modifier onlyApprovedIssuer() {
        if (!approvedIssuers[msg.sender]) {
            revert NotApprovedIssuer(msg.sender);
        }
        _;
    }

    constructor(uint256 initialDailyIssuerSubmissionLimit) Ownable(msg.sender) {
        _setDailyIssuerSubmissionLimit(initialDailyIssuerSubmissionLimit);
        approvedIssuers[msg.sender] = true;
        emit IssuerAdded(msg.sender);
    }

    function addIssuer(address issuer) external onlyOwner {
        if (issuer == address(0)) {
            revert InvalidIssuer(issuer);
        }
        if (approvedIssuers[issuer]) {
            revert IssuerAlreadyApproved(issuer);
        }

        approvedIssuers[issuer] = true;
        emit IssuerAdded(issuer);
    }

    function removeIssuer(address issuer) external onlyOwner {
        if (!approvedIssuers[issuer]) {
            revert IssuerNotApproved(issuer);
        }

        approvedIssuers[issuer] = false;
        emit IssuerRemoved(issuer);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function setDailyIssuerSubmissionLimit(uint256 newLimit) external onlyOwner {
        _setDailyIssuerSubmissionLimit(newLimit);
    }

    function renounceOwnership() public pure override {
        revert OwnershipRenunciationDisabled();
    }

    function submitScoreRoot(
        bytes32 userHash,
        bytes32 scoreEventHash,
        bytes32 merkleRoot,
        bytes32 modelVersionHash,
        uint32 scorePeriod
    ) external onlyApprovedIssuer whenNotPaused {
        _requireNonZeroHash(userHash, "userHash");
        _requireNonZeroHash(scoreEventHash, "scoreEventHash");
        _requireNonZeroHash(merkleRoot, "merkleRoot");
        _requireNonZeroHash(modelVersionHash, "modelVersionHash");
        _validateScorePeriod(scorePeriod);

        if (usedScoreEventHashes[scoreEventHash]) {
            revert ScoreEventHashAlreadyUsed(scoreEventHash);
        }

        LatestScoreRecord storage latestRecord = latestRecordByUserHash[userHash];

        if (latestRecord.timestamp != 0) {
            if (scorePeriod <= latestRecord.scorePeriod) {
                revert ScorePeriodNotNewer(scorePeriod, latestRecord.scorePeriod);
            }

            uint256 nextAllowedTimestamp =
                uint256(latestRecord.timestamp) + MIN_SCORE_UPDATE_INTERVAL;

            if (block.timestamp < nextAllowedTimestamp) {
                revert ScoreUpdateTooSoon(nextAllowedTimestamp);
            }
        }

        _consumeDailyIssuerSubmission(msg.sender);
        usedScoreEventHashes[scoreEventHash] = true;

        latestRecordByUserHash[userHash] = LatestScoreRecord({
            scoreEventHash: scoreEventHash,
            merkleRoot: merkleRoot,
            modelVersionHash: modelVersionHash,
            timestamp: uint64(block.timestamp),
            scorePeriod: scorePeriod,
            issuer: msg.sender
        });

        emit ScoreRootSubmitted(
            userHash,
            scoreEventHash,
            merkleRoot,
            modelVersionHash,
            scorePeriod,
            block.timestamp,
            msg.sender
        );
    }

    function issuerDailyUsage(
        address issuer
    ) external view returns (uint256 day, uint256 submissions, uint256 remaining) {
        day = block.timestamp / 1 days;
        DailyIssuerUsage memory usage = _dailyUsageByIssuer[issuer];

        if (usage.day == day) {
            submissions = usage.submissions;
        }

        if (submissions < dailyIssuerSubmissionLimit) {
            remaining = dailyIssuerSubmissionLimit - submissions;
        }
    }

    function nextSubmissionAt(bytes32 userHash) external view returns (uint256) {
        uint256 latestTimestamp = latestRecordByUserHash[userHash].timestamp;

        if (latestTimestamp == 0) {
            return 0;
        }

        return latestTimestamp + MIN_SCORE_UPDATE_INTERVAL;
    }

    function verifyScoreEvent(
        bytes32[] calldata proof,
        bytes32 root,
        bytes32 leaf
    ) external pure returns (bool) {
        return MerkleProof.verifyCalldata(proof, root, leaf);
    }

    function _setDailyIssuerSubmissionLimit(uint256 newLimit) private {
        if (newLimit == 0 || newLimit > type(uint192).max) {
            revert InvalidDailyIssuerSubmissionLimit(newLimit);
        }

        uint256 previousLimit = dailyIssuerSubmissionLimit;
        dailyIssuerSubmissionLimit = newLimit;
        emit DailyIssuerSubmissionLimitUpdated(previousLimit, newLimit);
    }

    function _consumeDailyIssuerSubmission(address issuer) private {
        uint64 currentDay = uint64(block.timestamp / 1 days);
        DailyIssuerUsage storage usage = _dailyUsageByIssuer[issuer];

        if (usage.day != currentDay) {
            usage.day = currentDay;
            usage.submissions = 0;
        }

        if (usage.submissions >= dailyIssuerSubmissionLimit) {
            revert DailyIssuerSubmissionLimitReached(issuer, dailyIssuerSubmissionLimit);
        }

        usage.submissions += 1;
    }

    function _validateScorePeriod(uint32 scorePeriod) private pure {
        uint32 year = scorePeriod / 100;
        uint32 month = scorePeriod % 100;

        if (year < 2020 || month == 0 || month > 12) {
            revert InvalidScorePeriod(scorePeriod);
        }
    }

    function _requireNonZeroHash(bytes32 value, string memory field) private pure {
        if (value == bytes32(0)) {
            revert InvalidHash(field);
        }
    }
}
