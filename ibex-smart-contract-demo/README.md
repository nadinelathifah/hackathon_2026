# Ibex Smart Contract Demo

Ibex Credit is a universal credit rating platform for internationals in the UK. This demo shows how Ibex can use Polygon as a tamper-evident audit layer for ML-generated credit score updates without putting personal or financial data on-chain.

For a simple laptop-to-Polygon walkthrough, see [`TEAMMATE_SMART_CONTRACT_GUIDE.md`](./TEAMMATE_SMART_CONTRACT_GUIDE.md).

The V1 demo contract is deployed on **Polygon PoS mainnet**. It has also been exercised with a mock credit score event whose proof was submitted, read back, and successfully verified against the original off-chain event. `ScoreAuditRegistryV2` is implemented and tested in this repository but has not yet been deployed to mainnet.

Ibex uses Polygon as a tamper-evident audit layer. The ML-generated score and user financial data remain off-chain. When a score is generated or updated, the backend creates a score event hash and Merkle root. The smart contract stores those proofs on Polygon. Later, anyone with permission to view the off-chain record can recompute the hash and verify that the score event has not been changed.

## What This Demo Does

The local demo performs the full proof flow:

1. Creates a mock credit score event off-chain.
2. Converts the event into canonical JSON.
3. Hashes the canonical event with `keccak256`.
4. Creates a salted `userHash`.
5. Hashes the ML model version.
6. Creates a Merkle root.
7. Submits only the hashes and root to `ScoreAuditRegistry`.
8. Reads the latest audit record for that user.
9. Recomputes the off-chain hashes and verifies that they still match the on-chain proof.

## Why Ibex Uses Polygon

Polygon gives Ibex a low-cost public audit layer. The blockchain is useful here because it makes proof records hard to alter after submission. Ibex does not need Polygon to run credit scoring, store user profiles, or process bank data. Polygon is used only to anchor cryptographic commitments that can later prove whether an off-chain score event has changed.

This design keeps the sensitive product logic and customer data off-chain while still giving Ibex, lenders, auditors, and users a shared source of truth for proof timestamps and issuer addresses.

## How A Website User Interacts With Ibex

The user does not submit blockchain transactions and does not upload JSON to Polygon. Their interaction is with the Ibex website and authenticated backend:

1. The user signs in and requests or views their monthly credit score.
2. The backend confirms that the account is eligible for the current scoring period.
3. The ML service calculates the score and creates a private score-event JSON document.
4. Ibex stores that JSON in its protected off-chain database or object storage.
5. The backend canonicalises and hashes the event, creates the salted `userHash`, and builds the Merkle root.
6. A queued blockchain worker uses an approved Ibex issuer wallet to submit only the proof to Polygon.
7. The backend stores the transaction hash and shows the score to the user from the private database.
8. During verification, the backend recomputes the hashes and compares them with the public Polygon proof.

```text
User -> Ibex website -> authenticated API -> ML model -> private database
                                           -> hashing worker -> issuer wallet -> Polygon
```

The issuer private key must exist only in the protected backend worker. It must never be sent to the browser. If Ibex pays transaction fees, the API must authenticate users, reject duplicate monthly jobs, rate-limit requests, and queue submissions before the issuer wallet signs anything.

## What Is Stored On-Chain

The smart contract stores the latest audit record for each `userHash`:

```solidity
struct LatestScoreRecord {
    bytes32 scoreEventHash;
    bytes32 merkleRoot;
    bytes32 modelVersionHash;
    uint256 timestamp;
    address issuer;
}
```

V1 stores only these values on-chain:

- `userHash`
- `scoreEventHash`
- `merkleRoot`
- `modelVersionHash`
- `timestamp`
- `issuer address`

V2 keeps the same proof data and adds the non-personal `scorePeriod` value, such as `202608`, plus minimal anti-abuse state for used event hashes and per-issuer daily counters. It still does not store the score or source JSON.

## What Is Kept Off-Chain

The following data must stay off-chain:

- actual credit score
- user identity
- user ID
- bank transactions
- income details
- visa details
- address
- ML features
- model inputs and explanations

The demo includes a mock event so the flow is easy to understand, but that event is never submitted to the contract. Only its cryptographic hash is submitted.

## How Score Event Hashing Works

The hashing utility in `utils/hashScoreEvent.js` creates a mock event, serializes it into canonical JSON with sorted object keys, and hashes that canonical string:

```javascript
scoreEventHash = keccak256(canonicalScoreEventJson)
```

The user identifier is also not stored directly. The demo creates:

```text
userHash = keccak256(userId + ":" + userSalt)
```

For production, the salt should be managed securely by the Ibex backend. The demo uses a deterministic salt so every script can reproduce the same proof.

The model version is committed as:

```text
modelVersionHash = keccak256("ibex-credit-model-v1.0")
```

## How Merkle Roots Are Used

For the simplest single-event demo:

```text
merkleRoot = scoreEventHash
```

That makes local verification easy to follow. The utility also includes an optional batch Merkle tree implementation using `merkletreejs`. In a production batch, Ibex could hash many score events, build a Merkle tree, submit one root, and later verify individual events using Merkle proofs.

## Live Polygon Mainnet Deployment

The current demo deployment is live on Polygon PoS mainnet:

| Item | Value |
| --- | --- |
| Network | Polygon PoS mainnet |
| Chain ID | `137` |
| Deployment date | `2026-07-15` |
| Contract | [`0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534`](https://polygonscan.com/address/0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534) |
| Deployer, owner, and first approved issuer | `0x4bCa26d44634966C75abdBCec41DDf94a930a49c` |
| Submitted proof transaction | [`0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4`](https://polygonscan.com/tx/0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4) |

The mock score audit record currently stored for the demo user is:

```text
userHash:         0x71e0e7f1941b16b4090d876f9362b6171f94f3df9b29892f971feca7b3771d8f
scoreEventHash:   0xe4aca8bd24eb49bece24ef709a864d19ce45276d4327f562585c264eec5fe3f4
merkleRoot:       0xe4aca8bd24eb49bece24ef709a864d19ce45276d4327f562585c264eec5fe3f4
modelVersionHash: 0x9e18414b8d423076ad0f886c4f0dcee62a353767559cb930de732216fac76f45
timestamp:        1784122751 (2026-07-15T13:39:11.000Z)
issuer:           0x4bCa26d44634966C75abdBCec41DDf94a930a49c
```

Recreating the mock event off-chain and running the verification script returned:

```text
Verification result: VALID
The off-chain score event matches the on-chain audit proof.
```

## ScoreAuditRegistryV2 Abuse Protections

V2 is in `contracts/ScoreAuditRegistryV2.sol`. It preserves V1 so the existing deployment and proof history remain available.

V2 adds:

- approved-issuer-only submissions
- one strictly newer `YYYYMM` score period for each stable `userHash`
- a 28-day safety cooldown between successful updates for the same user hash
- global duplicate `scoreEventHash` rejection
- a configurable daily successful-submission limit for each issuer wallet
- owner-controlled emergency pause and unpause
- owner-controlled issuer addition and removal
- OpenZeppelin two-step ownership transfer
- disabled ownership renunciation to prevent accidentally leaving the contract ownerless
- rejection of zero hashes and malformed score periods

The scoring period is normally derived from the score event's UTC timestamp. `SCORE_PERIOD` can override it when needed:

```text
SCORE_PERIOD=202608
```

The same stable backend `USER_SALT` must be used for a user over time. Rotating the salt changes the `userHash` and would make the contract treat that person as a new pseudonymous user.

V2 deliberately does not automatically ban a website user. Polygon sees the approved backend issuer wallet, not the human operating the website. User bans, HTTP rate limits, account lockouts, and idempotency keys belong in the authenticated backend. The contract can pause all submissions or the owner can remove a compromised issuer.

The daily contract limit counts successful submissions per issuer per UTC day. It is a circuit breaker, not a substitute for backend controls. A stolen issuer private key can transfer the wallet's POL directly, so production issuer wallets must be isolated, tightly funded, monitored, and never exposed to the frontend.

## Install

```bash
npm install
```

## Run The Local Demo

Compile the contract:

```bash
npm run compile
```

Run tests:

```bash
npm run test
```

Run the full local proof flow:

```bash
npm run demo
```

Run the protected V2 flow:

```bash
npm run demo:v2
```

The V2 demo submits one proof, verifies it, confirms that a duplicate submission is rejected, and prints the issuer's remaining daily allowance.

Expected output includes:

```text
Ibex Smart Contract Demo Started

1. Mock score event created off-chain
2. userHash generated: 0x...
3. scoreEventHash generated: 0x...
4. modelVersionHash generated: 0x...
5. Merkle root generated: 0x...
6. Score proof submitted to smart contract
7. Latest record read from smart contract
8. Verification result: VALID

No personal data was stored on-chain.
Only hashes, Merkle root, timestamp, and issuer address were stored.
```

## Use An ML-Generated Score Event

The local demo, Polygon submission, read, and verification scripts can use a JSON event produced by a machine-learning service. If `SCORE_EVENT_FILE` is not configured, they continue to use the built-in mock event.

Start from the included example:

```bash
cp examples/score-event.example.json score-event.json
```

`score-event.json` is ignored by Git because a real event can contain private score information. The `.pkl` model, raw financial data, and ML features must also remain off-chain and outside Git.

The custom JSON must include at least:

```text
userId
newScore
timestamp
modelVersion
```

It may include the score band, confidence, previous score, and positive or negative factors. The complete JSON object is canonicalised and hashed, so verification must use the exact same file.

Generate a private user salt:

```bash
openssl rand -hex 32
```

Add the event path and generated salt to `.env`:

```text
SCORE_EVENT_FILE=./score-event.json
USER_SALT=your_generated_secret_salt
```

Do not commit either the real event or its salt. Test the ML event locally without a wallet or gas:

```bash
npm run demo
```

The first output line should confirm that the event was loaded from `score-event.json`, and the final result should be `VALID`.

## Deploy To Polygon Mainnet

Polygon mainnet uses real POL for gas. Use a dedicated deployment wallet that you intentionally fund, and never paste its private key into chat or commit it to git. The project `.gitignore` excludes `.env`.

Create a local `.env` file:

```bash
cp .env.example .env
```

Fill in:

```text
PRIVATE_KEY=your_polygon_wallet_private_key
POLYGON_RPC_URL=https://polygon.drpc.org
SCORE_AUDIT_CONTRACT_ADDRESS=
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
SCORE_EVENT_FILE=
USER_SALT=
ISSUER_ADDRESS=
```

`PRIVATE_KEY` must be the private key for the funded deployment account, not its public `0x...` wallet address. The selected network in the MetaMask interface does not control these scripts; Hardhat connects to Polygon mainnet using `POLYGON_RPC_URL` and chain ID `137`.

Check the configured wallet, chain, and POL balance before spending funds:

```bash
npm run wallet:polygon
```

For the live deployment, this confirmed:

```text
Network: polygon
Chain ID: 137
Wallet address: 0x4bCa26d44634966C75abdBCec41DDf94a930a49c
Balance: 15.28845216 POL
```

Deploy the registry:

```bash
npm run deploy:polygon
```

The live deployment returned:

```text
ScoreAuditRegistry deployed to: 0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
```

Add that address to `.env`:

```text
SCORE_AUDIT_CONTRACT_ADDRESS=0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
```

Submit the mock score proof:

```bash
npm run submit:polygon
```

The live submission returned:

```text
Transaction sent: 0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4
Score proof anchored on Polygon mainnet
PolygonScan link: https://polygonscan.com/tx/0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4
```

Read the record and verify it against the off-chain event:

```bash
npm run read:polygon
npm run verify:polygon
```

Every later call to `npm run submit:polygon` creates another real Polygon mainnet transaction and spends a small amount of POL. For the same `userHash`, the contract updates `latestRecordByUserHash`; the earlier transactions remain permanently visible in Polygon's transaction history.

### Deploy V2 To Polygon Mainnet

Do not reuse the V1 address for V2. Before deploying, choose a daily successful-submission limit based on expected volume and leave room for operational retries:

```text
V2_DAILY_ISSUER_LIMIT=1000
SCORE_AUDIT_V2_CONTRACT_ADDRESS=
```

Confirm the owner wallet and balance, then deploy:

```bash
npm run wallet:polygon
npm run deploy:v2:polygon
```

Deployment spends real POL. Copy the printed V2 address into `.env`:

```text
SCORE_AUDIT_V2_CONTRACT_ADDRESS=0xYourNewV2Address
```

Run the local V2 demo and full tests before any mainnet deployment:

```bash
npm run compile
npm run test
npm run demo:v2
```

Submit, read, and verify a V2 proof with:

```bash
npm run submit:v2:polygon
npm run read:v2:polygon
npm run verify:v2:polygon
```

V2 derives the scoring period from the event timestamp unless `SCORE_PERIOD=YYYYMM` is set. A second submission for the same period, a submission within 28 days, a reused event hash, a paused registry, or an exhausted issuer quota is rejected.

### Administer V2

The V2 admin script uses `V2_ADMIN_ACTION`. The configured `PRIVATE_KEY` must belong to the V2 owner for every action except `accept-ownership`.

```bash
# Add or remove the public address in ISSUER_ADDRESS
V2_ADMIN_ACTION=add-issuer npm run admin:v2:polygon
V2_ADMIN_ACTION=remove-issuer npm run admin:v2:polygon

# Emergency circuit breaker
V2_ADMIN_ACTION=pause npm run admin:v2:polygon
V2_ADMIN_ACTION=unpause npm run admin:v2:polygon

# Change the configurable daily issuer quota
V2_DAILY_ISSUER_LIMIT=1500 V2_ADMIN_ACTION=set-daily-limit npm run admin:v2:polygon
```

Ownership transfer requires two separate transactions. First, the current owner starts the transfer:

```bash
NEW_OWNER_ADDRESS=0xNewOwner V2_ADMIN_ACTION=transfer-ownership npm run admin:v2:polygon
```

Then the pending owner configures their own private key locally and accepts:

```bash
V2_ADMIN_ACTION=accept-ownership npm run admin:v2:polygon
```

For production, the intended owner should be a properly configured multisig rather than one person's general-purpose wallet.

## Approve Another Issuer Wallet

Only an approved issuer can submit score proofs. A teammate should send the contract owner only their public wallet address. They must never share their private key.

On the contract owner's laptop, set the teammate's public address in `.env`:

```text
ISSUER_ADDRESS=0xTeammatePublicWalletAddress
```

Make sure `PRIVATE_KEY` still belongs to the current contract owner, then run:

```bash
npm run add-issuer:polygon
```

The script checks that the configured signer is the contract owner, submits `addIssuer`, and prints the PolygonScan transaction link. If the address is already approved, it exits without spending gas.

After approval, the teammate uses their own `.env`, private key, and POL balance to submit proofs. The owner's private key never needs to leave the owner's laptop.

## Optional Polygon Amoy Deployment

Amoy remains configured as an optional testnet. It is not required to reproduce the live mainnet deployment described above.

Set these values in `.env` when Amoy test POL and an Amoy-compatible wallet are available:

```text
PRIVATE_KEY=your_testnet_wallet_private_key
AMOY_RPC_URL=https://polygon-amoy.drpc.org
SCORE_AUDIT_CONTRACT_ADDRESS=
AMOY_EXPLORER_BASE_URL=https://amoy.polygonscan.com
```

Then run:

```bash
npm run wallet:amoy
npm run deploy:amoy
```

Copy the deployed Amoy address into `SCORE_AUDIT_CONTRACT_ADDRESS` before submitting a testnet proof.

## Submit A Score Proof

Submit to the live Polygon mainnet deployment:

```bash
npm run submit:polygon
```

When `SCORE_EVENT_FILE` and `USER_SALT` are set, this submits the ML-generated event's hashes. The script prints the derived `userHash`, event hash, model hash, Merkle root, and Polygon transaction link. It never submits the JSON content itself.

To use the optional Amoy deployment instead:

```bash
npm run submit:amoy
```

The script prints:

```text
Transaction sent: 0x...
Score proof anchored on Polygon Amoy
PolygonScan link: https://amoy.polygonscan.com/tx/0x...
```

## Read The Latest Score Record

Read the latest record for the configured score event from Polygon mainnet:

```bash
npm run read:polygon
```

The script prints:

```text
Latest score audit record:
scoreEventHash: 0x...
merkleRoot: 0x...
modelVersionHash: 0x...
timestamp: ...
issuer: 0x...
```

When `SCORE_EVENT_FILE` and `USER_SALT` are configured, the script derives the same `userHash` automatically. To read another user hash directly, set `USER_HASH` when running the script:

```bash
USER_HASH=0x... npm run read:polygon
```

## Verify A Score Proof

Verify that the configured off-chain score event still matches the on-chain proof:

```bash
npm run verify:polygon
```

Valid output:

```text
Verification result: VALID
The off-chain score event matches the on-chain audit proof.
```

Invalid output:

```text
Verification result: INVALID
The off-chain score event does not match the on-chain audit proof.
Possible tampering detected.
```

## Contract Permissions

`ScoreAuditRegistry` has a simple issuer model:

- The deployer is the contract `owner`.
- The deployer is also the first approved issuer.
- Only the owner can add or remove issuers.
- Only approved issuers can submit score audit roots.

This keeps the demo readable while still showing the core production idea: Ibex-approved backend wallets can anchor score proofs, and unapproved wallets cannot.

`ScoreAuditRegistryV2` adds two-step ownership, emergency pausing, strict score periods, a cooldown, duplicate detection, and configurable issuer quotas. A new owner is not automatically made an issuer; issuer permission remains a separate explicit decision.

## Privacy And Data Minimization

No personal financial data is stored on-chain. This matters because blockchain state is public and long-lived. Storing raw identity, credit, bank, income, visa, address, or ML feature data on-chain would create serious privacy and compliance problems.

Ibex avoids that by storing only cryptographic commitments. A hash proves that a specific off-chain record existed in a specific form, but it does not reveal the record itself. Verification happens by recomputing the hash from the permissioned off-chain data and comparing it with the on-chain proof.

## Project Structure

```text
ibex-smart-contract-demo/
  README.md
  package.json
  .gitignore
  .env.example
  hardhat.config.js

  contracts/
    ScoreAuditRegistry.sol
    ScoreAuditRegistryV2.sol

  scripts/
    addIssuer.js
    checkWallet.js
    deploy.js
    demoLocalFlow.js
    submitScoreRoot.js
    readLatestRecord.js
    verifyScoreEvent.js

    v2/
      deployV2.js
      demoLocalFlowV2.js
      submitScoreRootV2.js
      readLatestRecordV2.js
      verifyScoreEventV2.js
      manageRegistryV2.js

  test/
    ScoreAuditRegistry.test.js
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
```
