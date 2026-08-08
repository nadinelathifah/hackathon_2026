# Ibex Credit Hackathon Project

This repository keeps the two smart-contract versions in separate standalone Hardhat projects.

## Smart Contract Projects

| Folder | Purpose | Deployment status |
| --- | --- | --- |
| [`ibex-smart-contract-demo`](./ibex-smart-contract-demo/) | Original V1 audit registry and scripts | Deployed on Polygon mainnet |
| [`ibex-smart-contract-demo-v2`](./ibex-smart-contract-demo-v2/) | Protected V2 with monthly limits, duplicate prevention, issuer quotas, pausing, and two-step ownership | Implemented and tested, not yet deployed |

The existing V1 mainnet address is:

```text
0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
```

Do not use that address as the V2 address. V2 requires a separate deployment.

## Test V1

```bash
cd ibex-smart-contract-demo
npm install
npm run compile
npm run test
npm run demo
```

## Test V2

```bash
cd ibex-smart-contract-demo-v2
npm install
npm run compile
npm run test
npm run demo
```

Use Node.js 20 LTS or Node.js 22 LTS. Each folder has its own README, `.env.example`, package files, contract, scripts, utilities, and tests.

The ML model, private score-event JSON, user identity, financial data, user salt, `.env`, and wallet private keys must remain off-chain and must never be committed to Git.
