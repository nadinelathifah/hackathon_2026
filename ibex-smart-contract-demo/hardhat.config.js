require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

function getPrivateKeyAccounts() {
  if (!process.env.PRIVATE_KEY) {
    return [];
  }

  const privateKey = process.env.PRIVATE_KEY.trim();
  return [privateKey.startsWith("0x") ? privateKey : `0x${privateKey}`];
}

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: {
        enabled: true,
        runs: 200
      }
    }
  },
  networks: {
    hardhat: {},
    amoy: {
      url: process.env.AMOY_RPC_URL || "https://polygon-amoy.drpc.org",
      chainId: 80002,
      accounts: getPrivateKeyAccounts()
    },
    polygon: {
      url: process.env.POLYGON_RPC_URL || "https://polygon.drpc.org",
      chainId: 137,
      accounts: getPrivateKeyAccounts()
    }
  }
};
