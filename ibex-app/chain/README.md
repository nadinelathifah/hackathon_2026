# Ibex Score Audit Registry V2

This is the standalone Hardhat project for the protected V2 Ibex Credit score audit registry, vendored into the app repo so the all-in-one Docker image can anchor and verify on chain.

V2 was deployed to Polygon mainnet on 8 August 2026 after its local test and bytecode verification pass.

## Live Polygon Mainnet Deployment

| Item | Value |
| --- | --- |
| Network | Polygon PoS mainnet |
| Chain ID | `137` |
| Contract | [`0x8621D09F08C2f58803e7239F8D46D444e0eF63e1`](https://polygonscan.com/address/0x8621D09F08C2f58803e7239F8D46D444e0eF63e1) |
| Deployment transaction | [`0x270dc86632630a365b6316cdd875548ccf3e34a36038e2b24360efbd7fff83b6`](https://polygonscan.com/tx/0x270dc86632630a365b6316cdd875548ccf3e34a36038e2b24360efbd7fff83b6) |
| Owner and first issuer | `0x4bCa26d44634966C75abdBCec41DDf94a930a49c` |
| Daily successful-submission limit | `1000` per issuer per UTC day |

## V2 Protections

`ScoreAuditRegistryV2` adds:

- approved-issuer-only proof submission
- one strictly newer `YYYYMM` score period per stable `userHash`
- a 28-day minimum interval between successful updates for the same user hash
- duplicate `scoreEventHash` rejection across the registry
- a configurable daily successful-submission quota for each issuer
- emergency pause and unpause controls
- immediate owner-controlled issuer removal
- OpenZeppelin two-step ownership transfer
- disabled ownership renunciation
- zero-hash and malformed-period validation
- OpenZeppelin Merkle proof verification

## Data Boundary

V2 stores only `userHash`, `scoreEventHash`, `merkleRoot`, `modelVersionHash`, the non-personal `scorePeriod`, the Polygon timestamp, and the issuer address. It never stores the score, the band, a name, an email, bank data, ML features, or the salt.

## Install And Test

Use Node.js 20 LTS.

```bash
cd chain
npm install
npm run compile
npm run test
npm run demo
```

Expected test result: `33 passing`. The local demo deploys V2 on Hardhat, submits a mock proof, verifies it, and confirms a duplicate is rejected — no wallet, `.env`, or POL needed.

## Configuration

Locally, copy `.env.example` to `.env` and fill it in. In Docker/Render, the same names are supplied as environment variables — `dotenv` does not override variables that already exist, so dashboard values win and no `.env` file is needed in the image.

Never place a real key in `.env.example`, Git, screenshots, chat, frontend code, or a Docker image.
