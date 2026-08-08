# Ibex Credit: Teammate Smart Contract Guide

This guide explains, in simple terms, how to connect the Ibex machine-learning score output to the smart contract.

It covers:

1. Running the project on your laptop.
2. Creating a score-event JSON file from the ML model.
3. Testing everything locally for free.
4. Getting your wallet approved by the contract owner.
5. Submitting the score proof to Polygon mainnet.
6. Reading and verifying the proof.

## 1. What The System Does

The ML model generates a score privately on your laptop.

The smart contract does not receive the model, score, or financial information. It receives only cryptographic fingerprints of the score event.

```text
ML model generates score
        |
        v
Score saved as private JSON
        |
        v
Node.js creates cryptographic hashes
        |
        v
Hashes submitted to Polygon
        |
        v
Hashes can later verify the private JSON
```

This lets Ibex prove that a score event has not been changed without publishing the score itself.

## 2. What Stays Private

The following remains on the laptop or in the private Ibex database:

```text
ML .pkl file
actual credit score
score band and confidence
user identity
bank transactions
income information
ML input features
score explanations
complete score-event JSON
user salt
wallet private key
```

Polygon receives only:

```text
userHash
scoreEventHash
merkleRoot
modelVersionHash
Polygon timestamp
issuer wallet address
```

V2 also records a non-personal monthly period such as `202608` and minimal counters used to enforce anti-abuse limits. It still never receives the score-event JSON or financial data.

## 3. Current Polygon Contract

The Ibex demonstration contract is already deployed on Polygon PoS mainnet.

```text
Network: Polygon PoS mainnet
Chain ID: 137
Contract: 0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
```

PolygonScan:

```text
https://polygonscan.com/address/0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
```

The current contract owner is:

```text
0x4bCa26d44634966C75abdBCec41DDf94a930a49c
```

Only the owner can approve or remove issuer wallets. An approved issuer can submit score proofs but cannot control the contract.

This address is the V1 contract. V1 does not support ownership transfer. The repository now also contains `ScoreAuditRegistryV2`, which adds monthly submission protection, duplicate prevention, issuer quotas, emergency pausing, and safe two-step ownership transfer. V2 has not yet been deployed to Polygon mainnet, so do not use the V1 address with V2 scripts.

## How The User And Website Interact

The user does not call the Polygon write function directly. They sign in to the Ibex website and request or view their monthly score. The backend authenticates the user, checks the database for an existing score in that month, runs the model, stores the complete event privately, and places one proof submission in a queue.

An approved backend issuer wallet signs that queued Polygon transaction. The website never receives the issuer private key. The score shown to the user comes from the private database; Polygon provides a timestamped proof that the underlying event has not changed.

```text
User -> website -> authenticated backend -> ML model and private database
                                      -> proof queue -> issuer wallet -> Polygon
```

Website account bans, request throttling, and duplicate-job prevention happen in the backend. V2 independently rejects repeated score periods, updates inside its 28-day cooldown, reused event hashes, and submissions above an issuer's daily allowance.

## 4. Install The Project

Use Node.js 20 LTS or Node.js 22 LTS. Avoid Node.js 25 because Hardhat reports it as unsupported.

Clone the team repository:

```bash
git clone https://github.com/nadinelathifah/hackathon_2026.git
cd hackathon_2026
```

If the feature branch has not yet been merged into `main`, switch to it:

```bash
git switch feature/ibex-smart-contract-demo
```

Open the smart-contract project and install dependencies:

```bash
cd ibex-smart-contract-demo
npm install
```

Compile and run the tests:

```bash
npm run compile
npm run test
```

Expected result:

```text
42 passing
```

## 5. Create The Score Event

The Python ML code should create a JSON file after calculating a score.

The repository includes an example:

```text
examples/score-event.example.json
```

Copy it to a private working file:

```bash
cp examples/score-event.example.json score-event.json
```

`score-event.json` is ignored by Git and should not be committed.

A valid event looks like this:

```json
{
  "userId": "ibex-test-user-002",
  "previousScore": 690,
  "newScore": 735,
  "scoreBand": "Low Risk",
  "confidence": 0.82,
  "timestamp": "2026-07-30T14:00:00.000Z",
  "modelVersion": "ibex-credit-model-v1.0",
  "positiveFactors": [
    "Stable income deposits",
    "Rent paid consistently"
  ],
  "negativeFactors": [
    "Short UK financial history"
  ]
}
```

The required fields are:

```text
userId
newScore
timestamp
modelVersion
```

The timestamp must be a valid date string. `newScore` must be a number.

Do not put names, addresses, bank details, visa information, or raw ML features in this file.

The Python model can save the event using code similar to:

```python
import json
from datetime import datetime, timezone

score_event = {
    "userId": "ibex-test-user-002",
    "previousScore": 690,
    "newScore": int(predicted_score),
    "scoreBand": score_band,
    "confidence": float(confidence),
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "modelVersion": "ibex-credit-model-v1.0",
    "positiveFactors": positive_factors,
    "negativeFactors": negative_factors,
}

with open("score-event.json", "w", encoding="utf-8") as file:
    json.dump(score_event, file, indent=2)
```

The private `.pkl` model does not need to be copied into the smart-contract repository.

## 6. Configure The Local Event

Create a private `.env` file:

```bash
cp .env.example .env
```

Generate a private user salt:

```bash
openssl rand -hex 32
```

Copy the generated value into `.env`:

```text
SCORE_EVENT_FILE=./score-event.json
USER_SALT=paste_the_generated_salt_here
```

Keep this salt. The same event and salt are required to recreate the same `userHash` during verification.

Never commit `.env`, `score-event.json`, the user salt, or a wallet private key.

## 7. Test Locally First

Run:

```bash
npm run demo
```

This test:

1. Reads `score-event.json`.
2. Validates the required fields.
3. Creates the hashes.
4. Deploys a temporary contract on the local Hardhat network.
5. Submits the hashes to the temporary contract.
6. Reads the hashes back.
7. Verifies that the JSON still matches.

This test requires no wallet, no internet, and no POL.

Expected output includes:

```text
Score event loaded off-chain from: .../score-event.json
Verification result: VALID
```

Do not continue to Polygon until the local result is `VALID`.

## 8. Prepare A Wallet For Polygon

Use a dedicated development wallet rather than a wallet holding significant funds.

The wallet needs:

```text
a public address
its own private key stored only on this laptop
a small POL balance on Polygon mainnet
```

Send the contract owner only the public wallet address:

```text
0xYourPublicWalletAddress
```

Never send anyone the private key or seed phrase.

## 9. Owner Approves The Teammate Wallet

This step happens on the contract owner's laptop, not the teammate's laptop.

The owner adds the teammate's public address to the owner's private `.env`:

```text
PRIVATE_KEY=owners_existing_private_key
POLYGON_RPC_URL=https://polygon.drpc.org
SCORE_AUDIT_CONTRACT_ADDRESS=0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
ISSUER_ADDRESS=0xTeammatePublicWalletAddress
```

The owner checks the configured wallet:

```bash
npm run wallet:polygon
```

The wallet address must match the current contract owner:

```text
0x4bCa26d44634966C75abdBCec41DDf94a930a49c
```

The owner then runs:

```bash
npm run add-issuer:polygon
```

This sends a real Polygon transaction and spends a small amount of POL.

The command prints:

```text
Transaction sent: 0x...
Approved issuer: 0xTeammatePublicWalletAddress
PolygonScan link: https://polygonscan.com/tx/0x...
```

If the wallet is already approved, the script exits without sending another transaction.

Approval does not transfer ownership. It only allows that wallet to submit score proofs.

## 10. Teammate Configures Their Wallet

After approval, the teammate completes their own `.env`:

```text
PRIVATE_KEY=teammates_private_key
POLYGON_RPC_URL=https://polygon.drpc.org
SCORE_AUDIT_CONTRACT_ADDRESS=0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
SCORE_EVENT_FILE=./score-event.json
USER_SALT=the_same_private_salt_used_for_this_event
```

Check the network, address, and balance:

```bash
npm run wallet:polygon
```

Expected network values:

```text
Network: polygon
Chain ID: 137
Wallet address: 0xTeammatePublicWalletAddress
Balance: some amount of POL
```

MetaMask's visible network selector does not control the script. Hardhat uses `POLYGON_RPC_URL` and chain ID `137`.

## 11. Submit The Proof To Polygon

Run:

```bash
npm run submit:polygon
```

The script:

1. Reads the private JSON.
2. Recreates its hashes.
3. Signs a transaction with the approved teammate wallet.
4. Sends only the hashes to the contract.
5. Waits for Polygon confirmation.
6. Prints the transaction link.

Expected output:

```text
Score event source: .../score-event.json
userHash: 0x...
scoreEventHash: 0x...
modelVersionHash: 0x...
merkleRoot: 0x...
Transaction sent: 0x...
Score proof anchored on Polygon mainnet
PolygonScan link: https://polygonscan.com/tx/0x...
```

This is a real mainnet transaction and spends POL.

## 12. Read The Stored Record

Run:

```bash
npm run read:polygon
```

Because the same event file and salt are configured, the script recreates the correct `userHash` automatically.

Expected output:

```text
Latest score audit record:
userHash: 0x...
scoreEventHash: 0x...
merkleRoot: 0x...
modelVersionHash: 0x...
timestamp: ...
issuer: 0xTeammatePublicWalletAddress
```

Reading is free and does not send a transaction.

## 13. Verify The Event

Run:

```bash
npm run verify:polygon
```

The script:

1. Reads the private JSON again.
2. Recalculates the hashes.
3. Reads the stored hashes from Polygon.
4. Compares them.

Expected result:

```text
Verification result: VALID
The off-chain score event matches the on-chain audit proof.
```

If someone changes any value in `score-event.json`, its hash changes and the result becomes:

```text
Verification result: INVALID
Possible tampering detected.
```

## 14. Updating A Score

When the ML model calculates a new score:

1. Create a new score-event JSON object.
2. Give it a new timestamp.
3. Keep the same private `userId` and `USER_SALT` for that user.
4. Test locally with `npm run demo`.
5. Submit with `npm run submit:polygon`.
6. Read and verify the new record.

The contract mapping stores the latest record for that `userHash`. Previous Polygon transactions and emitted events remain in the public blockchain history.

## 15. Common Errors

### `Not approved issuer`

The teammate's public address has not been approved, or the configured private key belongs to a different wallet.

The owner must run:

```bash
npm run add-issuer:polygon
```

with the correct `ISSUER_ADDRESS`.

### `USER_SALT must contain at least 16 characters`

Generate a proper salt:

```bash
openssl rand -hex 32
```

Put it in `.env` as `USER_SALT`.

### `Score event file is not valid JSON`

Check commas, quotation marks, brackets, and braces in `score-event.json`.

### Required score-event field error

Confirm the event contains:

```text
userId
newScore
timestamp
modelVersion
```

### Wallet balance is `0 POL`

The wallet has no Polygon mainnet POL, or the private key belongs to a different address.

### Verification is `INVALID`

Confirm that:

- the event file is exactly the one originally submitted;
- `USER_SALT` is unchanged;
- the correct contract address is configured;
- the correct Polygon network is configured; and
- a later event has not replaced the latest record for that user.

### Hardhat reports an unsupported Node.js version

Install and use Node.js 20 LTS or Node.js 22 LTS.

## 16. Security Checklist

Before every Polygon submission, confirm:

- `.env` is not tracked by Git;
- `score-event.json` is not tracked by Git;
- the `.pkl` model is private;
- no raw transactions or personal data are in the event;
- the wallet address is approved;
- the wallet contains only a limited amount of POL;
- chain ID is `137`;
- the contract address is correct; and
- the local demo returns `VALID`.
- the website API authenticated and rate-limited the request; and
- the issuer private key was never exposed to the website or browser.

## 17. Command Summary

### Teammate local test

```bash
cd ibex-smart-contract-demo
npm install
cp examples/score-event.example.json score-event.json
cp .env.example .env
openssl rand -hex 32
# Add SCORE_EVENT_FILE and USER_SALT to .env
npm run compile
npm run test
npm run demo
```

### Contract owner approves teammate

```bash
# Add teammate public address as ISSUER_ADDRESS in owner's .env
npm run wallet:polygon
npm run add-issuer:polygon
```

### Teammate submits and verifies

```bash
# Add teammate PRIVATE_KEY and Polygon settings to teammate's .env
npm run wallet:polygon
npm run submit:polygon
npm run read:polygon
npm run verify:polygon
```

## 18. V2 Protected Contract Commands

V2 must be deployed to a new address. Run its local demo first:

```bash
npm run compile
npm run test
npm run demo:v2
```

Before a real deployment, set a planned daily issuer limit in `.env`:

```text
V2_DAILY_ISSUER_LIMIT=1000
SCORE_AUDIT_V2_CONTRACT_ADDRESS=
```

Deploying spends real POL:

```bash
npm run wallet:polygon
npm run deploy:v2:polygon
```

Put the new address into `SCORE_AUDIT_V2_CONTRACT_ADDRESS`. Do not put the existing V1 address there. V2 operations then use:

```bash
npm run submit:v2:polygon
npm run read:v2:polygon
npm run verify:v2:polygon
```

The score period comes from the JSON timestamp. It can be set explicitly as `SCORE_PERIOD=202608`. Keep the same private `USER_SALT` so a user receives the same stable `userHash` each month.

The V2 owner can manage safeguards with:

```bash
V2_ADMIN_ACTION=add-issuer npm run admin:v2:polygon
V2_ADMIN_ACTION=remove-issuer npm run admin:v2:polygon
V2_ADMIN_ACTION=pause npm run admin:v2:polygon
V2_ADMIN_ACTION=unpause npm run admin:v2:polygon
V2_DAILY_ISSUER_LIMIT=1500 V2_ADMIN_ACTION=set-daily-limit npm run admin:v2:polygon
```

See `README.md` for the two-step ownership transfer commands and full V2 explanation.

## Final Summary

The model and score remain private. The Node.js scripts convert the score event into cryptographic hashes. An approved wallet submits those hashes to Polygon. The verification script later proves whether the private event still matches the public proof.
