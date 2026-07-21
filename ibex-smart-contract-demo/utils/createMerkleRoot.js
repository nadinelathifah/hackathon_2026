const { ethers } = require("ethers");
const { MerkleTree } = require("merkletreejs");

function hexToBuffer(value) {
  return Buffer.from(value.replace(/^0x/, ""), "hex");
}

function bufferToHex(value) {
  return `0x${value.toString("hex")}`;
}

function hashPair(value) {
  return hexToBuffer(ethers.keccak256(value));
}

function createBatchMerkleRoot(leaves) {
  if (!Array.isArray(leaves) || leaves.length === 0) {
    throw new Error("At least one leaf is required to create a Merkle root");
  }

  const leafBuffers = leaves.map(hexToBuffer);
  const tree = new MerkleTree(leafBuffers, hashPair, { sortPairs: true });
  const selectedLeaf = leafBuffers[0];

  return {
    merkleRoot: tree.getHexRoot(),
    proof: tree.getProof(selectedLeaf).map((entry) => bufferToHex(entry.data)),
    leaf: bufferToHex(selectedLeaf)
  };
}

function createMerkleRoot(scoreEventHash, batchLeaves = [scoreEventHash]) {
  if (!scoreEventHash) {
    throw new Error("scoreEventHash is required");
  }

  // Single-event demo: the Merkle root is the score event hash and the proof is empty.
  if (batchLeaves.length === 1) {
    return {
      merkleRoot: scoreEventHash,
      proof: [],
      leaf: scoreEventHash
    };
  }

  return createBatchMerkleRoot(batchLeaves);
}

module.exports = {
  createBatchMerkleRoot,
  createMerkleRoot
};
