// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

contract ScoreAuditRegistry {
    address public owner;

    mapping(address => bool) public approvedIssuers;

    struct LatestScoreRecord {
        bytes32 scoreEventHash;
        bytes32 merkleRoot;
        bytes32 modelVersionHash;
        uint256 timestamp;
        address issuer;
    }

    mapping(bytes32 => LatestScoreRecord) public latestRecordByUserHash;

    event IssuerAdded(address indexed issuer);
    event IssuerRemoved(address indexed issuer);

    event ScoreRootSubmitted(
        bytes32 indexed userHash,
        bytes32 indexed scoreEventHash,
        bytes32 merkleRoot,
        bytes32 modelVersionHash,
        uint256 timestamp,
        address indexed issuer
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "Not contract owner");
        _;
    }

    modifier onlyApprovedIssuer() {
        require(approvedIssuers[msg.sender], "Not approved issuer");
        _;
    }

    constructor() {
        owner = msg.sender;
        approvedIssuers[msg.sender] = true;
    }

    function addIssuer(address issuer) external onlyOwner {
        approvedIssuers[issuer] = true;
        emit IssuerAdded(issuer);
    }

    function removeIssuer(address issuer) external onlyOwner {
        approvedIssuers[issuer] = false;
        emit IssuerRemoved(issuer);
    }

    function submitScoreRoot(
        bytes32 userHash,
        bytes32 scoreEventHash,
        bytes32 merkleRoot,
        bytes32 modelVersionHash
    ) external onlyApprovedIssuer {
        latestRecordByUserHash[userHash] = LatestScoreRecord({
            scoreEventHash: scoreEventHash,
            merkleRoot: merkleRoot,
            modelVersionHash: modelVersionHash,
            timestamp: block.timestamp,
            issuer: msg.sender
        });

        emit ScoreRootSubmitted(
            userHash,
            scoreEventHash,
            merkleRoot,
            modelVersionHash,
            block.timestamp,
            msg.sender
        );
    }

    function verifyScoreEvent(
        bytes32[] calldata proof,
        bytes32 root,
        bytes32 leaf
    ) external pure returns (bool) {
        return MerkleProof.verifyCalldata(proof, root, leaf);
    }
}
