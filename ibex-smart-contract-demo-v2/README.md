# Ibex Score Audit Registry V2

This is the standalone Hardhat project for the protected V2 Ibex Credit score audit registry.

V1 remains in the sibling `ibex-smart-contract-demo` folder. V2 was deployed to Polygon mainnet on 8 August 2026 after its local test and bytecode verification pass.

## Live Polygon Mainnet Deployment

| Item | Value |
| --- | --- |
| Network | Polygon PoS mainnet |
| Chain ID | `137` |
| Contract | [`0x8621D09F08C2f58803e7239F8D46D444e0eF63e1`](https://polygonscan.com/address/0x8621D09F08C2f58803e7239F8D46D444e0eF63e1) |
| Deployment transaction | [`0x270dc86632630a365b6316cdd875548ccf3e34a36038e2b24360efbd7fff83b6`](https://polygonscan.com/tx/0x270dc86632630a365b6316cdd875548ccf3e34a36038e2b24360efbd7fff83b6) |
| Block | `91665099` |
| Confirmed at | `2026-08-08T15:24:36.000Z` |
| Owner and first issuer | `0x4bCa26d44634966C75abdBCec41DDf94a930a49c` |
| Daily successful-submission limit | `1000` per issuer per UTC day |
| Deployment fee | `0.358027829338392128 POL` |

The deployed runtime bytecode was read back from Polygon and exactly matched the local compiled `ScoreAuditRegistryV2` artifact. The registry was active, had no pending owner, and had the intended owner approved as its first issuer.

## How A User Interacts With Ibex

The user does not upload JSON to Polygon and does not call the contract's write function directly.

1. The user signs in to the Ibex website.
2. The website sends an authenticated monthly score request to the backend.
3. The backend checks whether that user already has a completed score for the month.
4. The ML service generates the score and a private score-event JSON document.
5. The backend stores the event in a protected off-chain database or object store.
6. A worker canonicalises and hashes the event and creates its Merkle root.
7. An approved, tightly funded issuer wallet submits only the proof to Polygon.
8. The website shows the private score from the database and stores the Polygon transaction reference.

```text
User -> website -> authenticated API -> ML model -> private database
                                  |
                                  +-> proof queue -> issuer wallet -> Polygon
```

The issuer private key must exist only in the protected blockchain worker. It must never be included in frontend code or sent to the browser.

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

The daily quota is a circuit breaker. It does not replace website authentication, API rate limits, monthly database checks, idempotency keys, a transaction queue, wallet alerts, or a small wallet balance.

V2 does not automatically ban the transaction caller because the caller is the Ibex issuer wallet, not the website user. Website-user throttling and temporary bans must happen in the backend using the authenticated account and request metadata.

## Data Boundary

V2 stores only:

- `userHash`
- `scoreEventHash`
- `merkleRoot`
- `modelVersionHash`
- non-personal `scorePeriod`
- Polygon timestamp
- issuer address
- minimal duplicate and issuer-quota state

V2 never stores:

- actual credit score or score band
- name, user ID, email, or address
- bank transactions or balances
- income or spending information
- visa or nationality information
- ML features or explanations
- model `.pkl` file
- complete score-event JSON
- user salt or wallet private key

## Install And Test

Use Node.js 20 LTS or Node.js 22 LTS.

```bash
cd ibex-smart-contract-demo-v2
npm install
npm run compile
npm run test
npm run demo
```

Expected test result:

```text
33 passing
```

The local demo deploys V2 on Hardhat, creates a private mock event, submits its proof, verifies it, and confirms that a duplicate is rejected. It requires no wallet, `.env`, or POL.

## Use An ML Score Event

Create a private working event from the included example:

```bash
cp examples/score-event.example.json score-event.json
cp .env.example .env
openssl rand -hex 32
```

Set the file and generated salt in `.env`:

```text
SCORE_EVENT_FILE=./score-event.json
USER_SALT=your_private_generated_salt
```

The same secret `USER_SALT` must be used consistently so each user receives the same stable `userHash` every month. The score period is derived from the event's UTC timestamp unless explicitly overridden:

```text
SCORE_PERIOD=202608
```

Neither `score-event.json` nor `.env` is tracked by Git.

## Polygon Configuration

Create `.env` and configure:

```text
PRIVATE_KEY=dedicated_wallet_private_key
POLYGON_RPC_URL=https://polygon.drpc.org
SCORE_AUDIT_V2_CONTRACT_ADDRESS=0x8621D09F08C2f58803e7239F8D46D444e0eF63e1
V2_DAILY_ISSUER_LIMIT=1000
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
```

Never place a real key in `.env.example`, Git, screenshots, chat, frontend code, or a Docker image.

Confirm the wallet and network:

```bash
npm run wallet:polygon
npm run estimate:polygon
npm run inspect:polygon
```

The estimate command reads current Polygon fee data and estimates deployment gas without sending a transaction.

## Deploy A New V2 Instance

The live V2 address is listed above. Run the deployment command again only when the team intentionally wants another contract instance. Choose `V2_DAILY_ISSUER_LIMIT` based on expected legitimate daily volume. The deployment script defaults to `1000`, but the team should explicitly review that value.

Deploying to Polygon mainnet spends real POL:

```bash
npm run deploy:polygon
```

Copy the new address printed by the script into:

```text
SCORE_AUDIT_V2_CONTRACT_ADDRESS=0xNewV2Address
```

Do not put the existing V1 address in this field.

## Submit, Read, And Verify

After deployment and issuer approval:

```bash
npm run submit:polygon
npm run read:polygon
npm run verify:polygon
```

Each successful submission spends POL from the configured approved issuer wallet. Reads and local verification do not create blockchain transactions.

## Admin Operations

Set `V2_ADMIN_ACTION` for each operation. Except for `accept-ownership`, the configured wallet must be the V2 owner.

```bash
# ISSUER_ADDRESS must contain the teammate's public wallet address
V2_ADMIN_ACTION=add-issuer npm run admin:polygon
V2_ADMIN_ACTION=remove-issuer npm run admin:polygon

# Emergency response
V2_ADMIN_ACTION=pause npm run admin:polygon
V2_ADMIN_ACTION=unpause npm run admin:polygon

# Configurable successful-submission quota per issuer per UTC day
V2_DAILY_ISSUER_LIMIT=1500 V2_ADMIN_ACTION=set-daily-limit npm run admin:polygon
```

Ownership transfer requires two transactions. The current owner starts it:

```bash
NEW_OWNER_ADDRESS=0xNewOwner V2_ADMIN_ACTION=transfer-ownership npm run admin:polygon
```

The pending owner then configures their own private key locally and accepts:

```bash
V2_ADMIN_ACTION=accept-ownership npm run admin:polygon
```

For production, ownership should be transferred to a properly tested multisig.

## Project Structure

```text
ibex-smart-contract-demo-v2/
  contracts/
    ScoreAuditRegistryV2.sol

  scripts/
    checkWallet.js
    estimateDeployment.js
    inspectRegistry.js
    deploy.js
    demoLocalFlow.js
    submitScoreRoot.js
    readLatestRecord.js
    verifyScoreEvent.js
    manageRegistry.js
    helpers.js

  test/
    ScoreAuditRegistryV2.test.js
    ScoreEventFile.test.js
    ScorePeriod.test.js

  utils/
    hashScoreEvent.js
    createMerkleRoot.js
    loadScoreEvent.js
    scorePeriod.js

  examples/
    score-event.example.json

  README.md
  package.json
  package-lock.json
  hardhat.config.js
  .env.example
  .gitignore
```

## Deployment Status

V2 is live at the address recorded above. No score-event proof has been submitted to V2 yet. The contract has passed the repository's automated tests and an exact deployed-bytecode comparison, but it has not received an independent security audit and must not yet be described as lender-production infrastructure.
