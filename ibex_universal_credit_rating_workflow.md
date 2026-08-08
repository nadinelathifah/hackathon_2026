# Ibex Credit: Universal Credit Rating Platform

## Project Workflow, Architecture, Setup, and Deployment Guide

Last updated: 2026-07-15

## 1. Project Summary

Ibex Credit is a universal credit rating platform for internationals in the UK. It is intended to create an explainable assessment from consented financial behaviour when a person has little or no conventional UK credit history.

The platform combines:

- consented financial data, initially synthetic for the public MVP;
- deterministic feature engineering;
- a privately hosted machine-learning model;
- an explainability layer;
- an encrypted off-chain application database; and
- Polygon PoS as a public, tamper-evident proof layer.

The core architecture rule is:

> Off-chain = users, private data, model execution, scores, explanations, and product logic.  
> On-chain = cryptographic proofs, timestamps, non-personal enforcement state, and issuer accountability.

Polygon does not calculate a score and does not store the model or any personal financial data. It records cryptographic commitments that allow an authorised party to prove later that an off-chain score event has not been changed.

## 2. Important Data-Minimisation Decision

The Ibex ML model does **not** require a separate onboarding questionnaire or demographic profile to generate a score.

The scoring pipeline must therefore not collect or use the following as model inputs merely for onboarding:

- name;
- nationality or country of origin;
- visa or immigration status;
- university;
- employer identity;
- home address;
- accommodation type;
- expected income entered by the user; or
- expected expenses entered by the user.

This removes the previous workflow step that combined Open Banking features with onboarding features.

The platform may still need a small amount of operational data for authentication, consent, support, or legal administration. That information must be kept separate from the model feature pipeline. For example, an email address used to sign in must not become a scoring feature.

For the public demonstration, users can select a synthetic financial profile and do not need to provide identity or bank information at all.

For a later real-user pilot, the preferred model input is a validated feature vector derived from consented financial data. The model service should receive only the feature names and values required by the approved model schema, plus a non-identifying request ID.

## 3. Current Project Status

The V1 blockchain demonstration is complete and running on Polygon PoS mainnet. A hardened `ScoreAuditRegistryV2` is implemented and tested in the repository but has not yet been deployed.

| Item | Current value |
| --- | --- |
| Network | Polygon PoS mainnet |
| Chain ID | `137` |
| Contract | [`0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534`](https://polygonscan.com/address/0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534) |
| Deployer, owner, and first issuer | `0x4bCa26d44634966C75abdBCec41DDf94a930a49c` |
| Demonstration proof transaction | [`0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4`](https://polygonscan.com/tx/0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4) |
| Verification result | `VALID` |

The contract project currently exists at:

```text
ibex-smart-contract-demo/
```

It includes both Solidity contract versions, hashing and Merkle utilities, V1 and V2 deployment/operation scripts, local demonstrations, and 42 passing tests.

The website, API, Python model service, PostgreSQL schema, anchoring worker, and Docker deployment still need to be implemented for the complete live MVP.

## 4. Recommended MVP Scope

The first public MVP should be a live, non-decisioning demonstration using synthetic financial data.

The public MVP should allow a visitor to:

1. Open the Ibex website.
2. Select a synthetic financial profile.
3. Run the real approved ML model against that profile's engineered features.
4. See the generated Ibex score, band, confidence, and explanation.
5. Anchor the score event proof on Polygon mainnet.
6. Read the stored proof back from the contract.
7. Recompute the event hash.
8. See a `VALID` or `INVALID` verification result.
9. Open the transaction on PolygonScan.

The public MVP should not be presented as a lender making an approval or rejection decision.

A closed real-user pilot is a later release gate. It requires consented data access, production security controls, privacy documentation, model governance, a review route, and confirmation of the applicable regulatory permissions.

## 5. End-to-End Workflow

```text
User opens Ibex
    |
    v
Synthetic profile selected OR consented financial data retrieved
    |
    v
Backend cleans and validates source data
    |
    v
Feature service creates the exact approved model feature vector
    |
    v
Python model service loads the verified .pkl artifact
    |
    v
ML model generates score output
    |
    v
Backend creates the complete off-chain score event
    |
    +--> Store score event privately in PostgreSQL
    |
    v
Canonicalise and hash the score event
    |
    v
Create userHash, modelVersionHash, and Merkle root
    |
    v
Approved issuer submits proof to Polygon
    |
    v
Store transaction receipt off-chain
    |
    v
Dashboard displays score and verification result
```

## 6. System Architecture

```mermaid
flowchart LR
    U[User Browser] --> W[Web Application]
    W --> A[Node API]
    A --> F[Feature Engineering]
    F --> M[Python ML Service]
    M --> A
    A --> D[(Encrypted PostgreSQL)]
    A --> Q[Anchoring Worker]
    Q --> P[Polygon ScoreAuditRegistry]
    P --> Q
    Q --> D
    A --> W

    O[Open Banking Provider] -. Closed pilot only .-> A
    S[Private Model Storage] --> M
    B[Off-site Backups] <-- Encrypted backups --> D
```

### Frontend

- product and consent information;
- synthetic profile selector for the public MVP;
- score dashboard;
- score explanation;
- score history;
- blockchain proof viewer;
- PolygonScan link; and
- review or correction request route for a real-user pilot.

### Node API

- authentication and sessions;
- consent records;
- source-data validation;
- feature engineering orchestration;
- model-service requests;
- canonical score-event construction;
- proof generation;
- database persistence;
- verification; and
- public API responses.

### Python ML Service

- verifies the approved model artifact before loading it;
- validates the feature schema;
- performs inference;
- creates model explanations;
- returns structured model output; and
- does not receive names, email addresses, addresses, visa data, or other onboarding information.

### Anchoring Worker

- accepts a completed score event reference from the API;
- calculates or confirms the required hashes;
- submits the proof using an approved issuer wallet;
- waits for the required confirmations;
- stores the transaction receipt; and
- retries recoverable failures without creating uncontrolled duplicate jobs.

### PostgreSQL

- stores the complete score event off-chain;
- stores encrypted operational user and consent records;
- stores model metadata and feature snapshots;
- stores Polygon transaction references; and
- supports later verification without exposing private information publicly.

## 7. Model Input Boundary

The model request should contain only the approved engineered features. The exact schema must be taken from the trained model and preprocessing pipeline rather than guessed from the product interface.

An example request shape is:

```json
{
  "requestId": "4c8835be-760d-4a16-8b33-b3ef6dd25b76",
  "featureSchemaVersion": "1.0",
  "features": {
    "averageMonthlyInflows": 2450.0,
    "incomeStability": 0.91,
    "spendingToIncomeRatio": 0.58,
    "rentPaymentConsistency": 1.0,
    "cashBufferMonths": 1.7,
    "overdraftDependency": 0.08,
    "transactionVolatility": 0.22
  }
}
```

These feature names are illustrative until the actual `.pkl` preprocessing schema is inspected and documented.

The model request must not include:

```text
name
email
phone
date of birth
nationality
visa status
university
employer name
home address
bank account number
raw transaction descriptions
wallet private key
```

The model response should be structured and versioned:

```json
{
  "score": 735,
  "scoreBand": "Low Risk",
  "confidence": 0.82,
  "positiveFactors": [
    "Stable income deposits",
    "Rent paid consistently",
    "Lower spending-to-income ratio"
  ],
  "negativeFactors": [
    "Short UK financial history"
  ],
  "modelVersion": "ibex-credit-model-v1.0",
  "modelArtifactHash": "0x...",
  "featureSchemaVersion": "1.0"
}
```

## 8. Hosting and Verifying the `.pkl` Model

The `.pkl` file remains off-chain and must never be uploaded to the smart contract, frontend, public Git repository, or public IPFS location.

For the first MVP it can be stored on an encrypted VPS volume outside the Git working tree. A private, versioned object store or model registry is preferred as the platform matures.

The model service must:

1. Read the model bytes from the approved location.
2. Calculate the model artifact hash.
3. Compare it with the expected approved hash.
4. Refuse to start when the hashes do not match.
5. Load the model only after verification.
6. Pin Python and package versions required by the model.
7. Expose the active model metadata through an internal health endpoint.

Python pickle files can execute code when loaded. Only a model produced by the trusted Ibex training pipeline may be loaded.

A model manifest should identify the complete model release:

```json
{
  "modelName": "ibex-credit-model",
  "modelVersion": "1.0",
  "artifactHash": "0x...",
  "featureSchemaHash": "0x...",
  "preprocessingVersion": "1.0",
  "trainingCodeCommit": "git-commit-sha",
  "pythonVersion": "3.11",
  "approvedAt": "2026-07-15T00:00:00Z"
}
```

For the current demonstration, `modelVersionHash` is the Keccak-256 hash of the model version string. For the production pipeline, it should be the Keccak-256 hash of the canonical model manifest so the proof identifies the exact model artifact and preprocessing release.

## 9. Score Event

After inference, the Node API creates the authoritative off-chain event. The model service should not construct the blockchain transaction.

Recommended event shape:

```json
{
  "schemaVersion": "1.0",
  "eventId": "ed837333-4979-40ae-a9c2-cb039f81995b",
  "userId": "internal-pseudonymous-uuid",
  "previousScore": 690,
  "newScore": 735,
  "scoreBand": "Low Risk",
  "confidence": 0.82,
  "timestamp": "2026-07-15T13:39:11Z",
  "modelVersion": "ibex-credit-model-v1.0",
  "modelArtifactHash": "0x...",
  "featureSchemaVersion": "1.0",
  "featureSnapshotHash": "0x...",
  "positiveFactors": [
    "Stable income deposits",
    "Rent paid consistently",
    "Lower spending-to-income ratio"
  ],
  "negativeFactors": [
    "Short UK financial history"
  ],
  "previousEventHash": "0x...",
  "commitmentNonce": "secret-random-value"
}
```

The complete object is private and stored off-chain. The random commitment nonce makes guessing a low-entropy score event from its public hash more difficult. It must remain off-chain.

## 10. Canonicalisation and Hashing

All services must agree on one canonical JSON implementation. Property order, numeric representation, missing values, and timestamp format must be deterministic.

The current JavaScript demonstration recursively sorts object keys before hashing. The production implementation should use a documented canonical JSON standard or a shared proof package with cross-language fixtures.

The proof values are:

```text
userHash = keccak256(internalUserId + ":" + secretUserSalt)

scoreEventHash = keccak256(canonicalScoreEventJson)

modelVersionHash = keccak256(canonicalModelManifestJson)

merkleRoot = scoreEventHash                    # single-event MVP

merkleRoot = root(batch(scoreEventHash[]))     # later batching
```

The demonstration uses a reproducible salt only so every local script creates the same result. Production must use a strong secret or keyed pseudonymisation scheme kept outside the source code.

## 11. Exact On-Chain and Off-Chain Boundary

### Stored on Polygon

Only the following proof values are stored by the deployed V1 `ScoreAuditRegistry`:

| On-chain value | Purpose |
| --- | --- |
| `userHash` | Pseudonymous lookup key; not a user ID or wallet address |
| `scoreEventHash` | Commitment to the complete private score event |
| `merkleRoot` | Commitment to one event or a batch of events |
| `modelVersionHash` | Commitment to the model version or model manifest |
| `timestamp` | Polygon block timestamp recorded by the contract |
| `issuer` | Approved wallet that submitted the proof |

V2 keeps those proof values and adds a non-personal `scorePeriod` such as `202608`, a used-event-hash flag, and per-issuer daily counters required for anti-abuse enforcement. It still does not store the score-event JSON, actual score, identity, or financial data.

### Kept off-chain

The following must never be written to Polygon:

| Off-chain data | Storage or handling rule |
| --- | --- |
| Actual score and score band | Encrypted score event in PostgreSQL |
| Confidence and explanations | Encrypted score event in PostgreSQL |
| User identity and contact details | Separate encrypted operational record, if required |
| Open Banking consent | Consent table with retention controls |
| Bank balances and transactions | Private ingestion pipeline; minimise retention |
| Engineered feature values | Encrypted feature snapshot or short-lived processing |
| Model `.pkl` artifact | Private model storage only |
| Model preprocessing code | Private application/model package |
| Feature schema | Private service configuration or model registry |
| User salt and commitment nonce | Secret storage only |
| Merkle proof for each event | Off-chain proof store |
| Polygon transaction receipt | Off-chain anchor record |
| Issuer private key | Secret manager or protected server secret |

### Data that should not be collected for scoring

Because the model does not need onboarding data, Ibex should not collect nationality, visa status, address, university, employer identity, or self-declared financial expectations as scoring inputs.

If a future non-scoring workflow has a justified need for any of these fields, it must have a separate purpose, access policy, retention period, and review. It must not silently become part of the model feature set.

## 12. Smart Contract Responsibilities

The deployed V1 contract is `ScoreAuditRegistry`. Its mainnet address and historical proof remain valid.

It provides:

- one owner;
- a mapping of approved issuer wallets;
- owner-controlled issuer addition and removal;
- issuer-only score-proof submission;
- the latest score record for each `userHash`;
- a `ScoreRootSubmitted` event for historical audit discovery; and
- OpenZeppelin Merkle proof verification.

The stored record is:

```solidity
struct LatestScoreRecord {
    bytes32 scoreEventHash;
    bytes32 merkleRoot;
    bytes32 modelVersionHash;
    uint256 timestamp;
    address issuer;
}
```

The public lookup mapping is:

```solidity
mapping(bytes32 => LatestScoreRecord) public latestRecordByUserHash;
```

The main write operation is:

```solidity
submitScoreRoot(
    bytes32 userHash,
    bytes32 scoreEventHash,
    bytes32 merkleRoot,
    bytes32 modelVersionHash
)
```

The contract does not receive a score, feature vector, identity record, or model artifact.

The repository also includes `ScoreAuditRegistryV2`. V2 adds:

- strictly newer `YYYYMM` score periods for each stable `userHash`;
- a 28-day minimum interval between successful updates for the same user hash;
- global duplicate score-event-hash rejection;
- a configurable daily successful-submission quota for each issuer;
- OpenZeppelin `Pausable` emergency controls;
- OpenZeppelin `Ownable2Step` ownership handover;
- disabled ownership renunciation;
- zero-hash and score-period validation; and
- the existing approved issuer and Merkle verification model.

The backend still enforces the exact calendar-month rule, authentication, account or IP rate limits, idempotency keys, and temporary user bans. The contract sees the approved issuer wallet rather than the website user, so automatically banning the transaction caller would incorrectly ban Ibex's own worker.

## 13. Current Polygon Mainnet Deployment

The live demonstration contract is:

```text
Network: Polygon PoS mainnet
Chain ID: 137
Contract: 0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
Owner: 0x4bCa26d44634966C75abdBCec41DDf94a930a49c
First approved issuer: 0x4bCa26d44634966C75abdBCec41DDf94a930a49c
```

Contract explorer:

```text
https://polygonscan.com/address/0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
```

The live mock proof transaction is:

```text
0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4
```

Transaction explorer:

```text
https://polygonscan.com/tx/0x4bc2b88411bd4d207d397d8fde35d8a31a6176c1ec9a51f5e50df852b70276e4
```

The stored demonstration proof is:

```text
userHash:         0x71e0e7f1941b16b4090d876f9362b6171f94f3df9b29892f971feca7b3771d8f
scoreEventHash:   0xe4aca8bd24eb49bece24ef709a864d19ce45276d4327f562585c264eec5fe3f4
merkleRoot:       0xe4aca8bd24eb49bece24ef709a864d19ce45276d4327f562585c264eec5fe3f4
modelVersionHash: 0x9e18414b8d423076ad0f886c4f0dcee62a353767559cb930de732216fac76f45
timestamp:        1784122751 (2026-07-15T13:39:11.000Z)
issuer:           0x4bCa26d44634966C75abdBCec41DDf94a930a49c
```

Recomputation returned:

```text
Verification result: VALID
The off-chain score event matches the on-chain audit proof.
```

## 14. Running the Existing Contract Project

Open the existing project:

```bash
cd "/Users/HP/Documents/IBEX Credit/ibex-smart-contract-demo"
```

Install, compile, test, and run the local proof flow:

```bash
npm install
npm run compile
npm run test
npm run demo
npm run demo:v2
```

Expected test result:

```text
9 passing
```

Expected demonstration result:

```text
Verification result: VALID

No personal data was stored on-chain.
Only hashes, Merkle root, timestamp, and issuer address were stored.
```

### Teammate reproduction guide

After cloning the team repository, teammates can reproduce the smart-contract demo with:

```bash
cd ibex-smart-contract-demo
npm install
npm run compile
npm run test
npm run demo
```

No wallet or `.env` file is required for compilation, tests, or the local Hardhat demonstration. `npm install` recreates `node_modules`, and Hardhat recreates `artifacts` and `cache`, so those generated directories must not be committed to Git.

For Polygon operations, each teammate must create their own local environment file:

```bash
cp .env.example .env
```

They must use their own wallet credentials and must never request, copy, commit, or share another team member's `.env` file or private key. The existing mainnet contract can be read by anyone, but only an approved issuer can submit a new proof. Any Solidity contract change must be compiled, tested, and deployed as a new contract because an already-deployed contract cannot be edited.

### Mainnet environment

Create `.env` from `.env.example` and set:

```text
PRIVATE_KEY=your_polygon_issuer_private_key
POLYGON_RPC_URL=https://polygon.drpc.org
SCORE_AUDIT_CONTRACT_ADDRESS=0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
SCORE_AUDIT_V2_CONTRACT_ADDRESS=
V2_DAILY_ISSUER_LIMIT=1000
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
```

Never put the real private key in `.env.example`, Git, documentation, screenshots, chat, or a Docker image.

Check the configured network and public wallet address:

```bash
npm run wallet:polygon
```

Submit another mock proof only when a new real mainnet transaction is intended:

```bash
npm run submit:polygon
```

Read and verify the current mock event:

```bash
npm run read:polygon
npm run verify:polygon
```

Every `submit:polygon` call spends POL. The latest mapping record is replaced for the same `userHash`, while earlier submissions remain visible through Polygon transaction and event history.

Polygon Amoy remains configured as an optional testnet, but the current project was actually deployed and demonstrated on Polygon mainnet.

V2 must use a new address. After local tests and review, its deployment flow is:

```bash
npm run compile
npm run test
npm run demo:v2
npm run wallet:polygon
npm run deploy:v2:polygon
```

Copy the resulting address into `SCORE_AUDIT_V2_CONTRACT_ADDRESS`. V2 submission, reading, and verification use `submit:v2:polygon`, `read:v2:polygon`, and `verify:v2:polygon`. V2 has not been deployed merely because these commands and scripts exist.

## 15. Contract Hardening Before a Real-User Pilot

The V1 deployment is appropriate for the public demonstration. V2 implements the first hardening pass, but it must still be independently reviewed and deployed to a clearly communicated new address before it becomes the audit registry for real users.

V1 has a fixed `owner` and no ownership-transfer function. V2 uses `Ownable2Step`, supports emergency pausing, rejects duplicate/monthly abuse, and limits each issuer's successful daily submissions.

Recommended production controls:

- owner held by a multisig or protected hardware-backed account;
- a separate server issuer wallet with limited POL;
- V2 ownership transferred to a tested multisig through its two-step flow;
- issuer rotation procedures;
- backend authentication, rate limits, monthly idempotency, and queue controls;
- a documented pause and incident-response decision;
- verified source code on PolygonScan;
- deployment record and checksum retained off-chain; and
- independent contract review before lender reliance.

The existing contract and its proof transaction should remain labelled as the Ibex mainnet demonstration. A new production address must be explicitly versioned and communicated rather than silently replacing it.

## 16. Off-Chain Database Design

Recommended PostgreSQL tables:

### `users`

Operational account record only. Suggested fields:

```text
id
auth_provider_subject
encrypted_contact_email (only when required)
status
created_at
updated_at
```

The model never queries this table directly.

### `consent_records`

```text
id
user_id
consent_type
policy_version
scope
granted_at
revoked_at
provider_reference
```

### `financial_connections`

Used only for a closed Open Banking pilot:

```text
id
user_id
provider
encrypted_provider_reference
status
consent_expires_at
last_refreshed_at
```

Do not store online-banking credentials.

### `feature_snapshots`

```text
id
user_id
schema_version
encrypted_features
feature_snapshot_hash
source_window_start
source_window_end
created_at
```

### `model_versions`

```text
id
model_name
model_version
artifact_hash
feature_schema_hash
manifest_json
approval_status
approved_at
retired_at
```

### `score_events`

```text
id
event_id
user_id
previous_event_id
encrypted_event_json
score_event_hash
model_version_id
status
created_at
```

### `blockchain_anchors`

```text
id
score_event_id
chain_id
contract_address
user_hash
score_event_hash
merkle_root
model_version_hash
transaction_hash
block_number
issuer_address
confirmation_count
anchor_status
submitted_at
confirmed_at
```

### `review_requests`

```text
id
score_event_id
user_id
reason
status
reviewer_id
decision_notes
created_at
resolved_at
```

### `audit_logs`

Record security-relevant application actions without logging raw financial data, full score events, access tokens, salts, nonces, or private keys.

## 17. Raw Financial Data Retention

The public MVP uses synthetic profiles and therefore does not need to retain real transactions.

For a real-user pilot:

1. Obtain explicit permission for the defined data scope.
2. Retrieve data through the approved provider.
3. Validate and categorise it in a private processing boundary.
4. Generate the approved model features.
5. Store only the minimum source data required for the product and review process.
6. Delete or expire raw data according to the documented retention policy.
7. Revoke access when consent is withdrawn or expires.

Raw transaction descriptions should not be sent to the ML service when engineered numeric or categorical features are sufficient.

## 18. API Design

### Public MVP endpoints

```text
GET  /api/health
GET  /api/demo/profiles
POST /api/demo/scores
GET  /api/scores/:scoreEventId
GET  /api/scores/:scoreEventId/verification
GET  /api/polygon/transactions/:transactionHash
```

### Closed-pilot endpoints

```text
POST   /api/auth/session
POST   /api/consents
DELETE /api/consents/:consentId
POST   /api/financial-connections
POST   /api/scores
GET    /api/scores
GET    /api/scores/:scoreEventId
POST   /api/scores/:scoreEventId/reviews
```

### Internal service endpoints

```text
GET  /internal/ml/health
GET  /internal/ml/model
POST /internal/ml/score
POST /internal/anchors
GET  /internal/anchors/:jobId
```

Internal endpoints must not be publicly routable from the internet.

## 19. Score Creation Transaction

The application-level score workflow should be treated as a state machine:

```text
FEATURES_READY
    -> MODEL_COMPLETE
    -> EVENT_STORED
    -> ANCHOR_PENDING
    -> TRANSACTION_SENT
    -> ANCHORED
    -> VERIFIED
```

Failure states should be explicit:

```text
MODEL_FAILED
ANCHOR_RETRYABLE
ANCHOR_FAILED
VERIFICATION_FAILED
```

The API should store the complete score event before asking the worker to anchor it. The worker should use an idempotency key based on `eventId` so a network retry cannot accidentally create uncontrolled duplicate jobs.

For the same `userHash`, a later score event updates the contract's latest record. The previous Polygon event remains part of the chain history, and the off-chain database links the event sequence using `previousEventHash`.

## 20. Target Repository Structure

The current repository contains only the contract demonstration. The full MVP should be organised as a monorepo or a small set of clearly versioned services.

Recommended target structure:

```text
ibex-platform/
  README.md
  .gitignore
  .env.example
  docker-compose.yml

  apps/
    web/
      Dockerfile
      package.json
      src/

  services/
    api/
      Dockerfile
      package.json
      src/
      migrations/

    ml/
      Dockerfile
      requirements.txt
      app/
      tests/
      models/                 # ignored by Git

  workers/
    anchor/
      Dockerfile
      package.json
      src/

  packages/
    proof/
      canonicalJson.js
      hashScoreEvent.js
      createMerkleRoot.js
      schemas/
      fixtures/

  blockchain/
    contracts/
    scripts/
    test/
    hardhat.config.js

  infra/
    caddy/
      Caddyfile
    postgres/
    backup/
    scripts/

  docs/
    architecture.md
    model-card.md
    incident-response.md
    data-retention.md
```

The existing `ibex-smart-contract-demo` can become `blockchain/` later, but it does not need to be moved until the full platform repository is created and its commands are preserved.

## 21. Environment Configuration

Create `.env.example` files containing names and safe placeholders only. Real values belong in a protected deployment environment.

### Shared application settings

```text
APP_ENV=development
PUBLIC_APP_URL=http://localhost:3000
API_BASE_URL=http://api:4000
LOG_LEVEL=info
```

### API settings

```text
DATABASE_URL=postgresql://ibex:change-me@postgres:5432/ibex
SESSION_SECRET=replace-with-strong-random-secret
FIELD_ENCRYPTION_KEY=replace-with-managed-encryption-key
USER_HASH_SECRET=replace-with-strong-secret
MODEL_SERVICE_URL=http://ml-service:8000
ANCHOR_WORKER_URL=http://anchor-worker:4100
```

### ML settings

```text
MODEL_PATH=/models/ibex-credit-model.pkl
MODEL_VERSION=ibex-credit-model-v1.0
MODEL_EXPECTED_ARTIFACT_HASH=0x...
FEATURE_SCHEMA_VERSION=1.0
```

### Polygon worker settings

```text
POLYGON_RPC_URL=https://polygon.drpc.org
POLYGON_CHAIN_ID=137
SCORE_AUDIT_CONTRACT_ADDRESS=0xD3da53b74Ce4d79d05D902059F8CC9Ec2a31e534
POLYGON_ISSUER_PRIVATE_KEY=replace-with-approved-issuer-key
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
POLYGON_CONFIRMATIONS=3
```

### PostgreSQL container settings

```text
POSTGRES_DB=ibex
POSTGRES_USER=ibex
POSTGRES_PASSWORD=replace-with-strong-password
```

Do not expose browser-facing variables with private values. Frontend build variables are public by design.

## 22. Local Development Prerequisites

Install or provide:

- Git;
- Node.js 20 LTS;
- npm;
- Python 3.11 or the version required by the approved model;
- Docker Engine or Docker Desktop;
- Docker Compose;
- the trusted `.pkl` artifact;
- its expected artifact hash;
- a synthetic feature fixture; and
- a Polygon RPC URL for integration testing.

The public MVP should be able to run end to end without Open Banking credentials by using synthetic profiles.

## 23. Local Setup Order for the Full MVP

The following is the required implementation and startup order once the full-stack files have been added.

### Step 1: Prepare the repository

```bash
git clone YOUR_PRIVATE_IBEX_REPOSITORY
cd ibex-platform
cp .env.example .env
```

Fill `.env` with development-only values. Do not reuse production secrets locally.

### Step 2: Add the model artifact

Place the trusted model at the configured private path:

```text
services/ml/models/ibex-credit-model.pkl
```

Add these patterns to `.gitignore`:

```text
.env
**/.env
**/models/*.pkl
**/models/*.pickle
backups/
node_modules/
__pycache__/
.venv/
```

Calculate the artifact hash and add the expected value to the local secret configuration. The ML service must fail closed if the file does not match.

### Step 3: Build containers

```bash
docker compose build
```

### Step 4: Start PostgreSQL

```bash
docker compose up -d postgres
docker compose ps
```

### Step 5: Apply database migrations

```bash
docker compose run --rm api npm run migrate
```

The exact migration command may differ with the selected Node ORM, but migrations must be committed and repeatable.

### Step 6: Start internal services

```bash
docker compose up -d ml-service api anchor-worker
```

Check:

```text
ML health: active model hash matches the approved hash
API health: database reachable
Worker health: Polygon chain ID is 137 and contract bytecode exists
```

### Step 7: Start the website and reverse proxy

```bash
docker compose up -d web caddy
docker compose ps
```

### Step 8: Run tests

```bash
docker compose run --rm ml-service pytest
docker compose run --rm api npm test
docker compose run --rm anchor-worker npm test
```

Run the existing contract suite separately:

```bash
cd blockchain
npm install
npm run compile
npm run test
npm run demo
```

### Step 9: Run one complete synthetic flow

```text
Select synthetic profile
    -> create approved feature vector
    -> run real model
    -> store private score event
    -> submit mainnet proof only when intentionally enabled
    -> read contract record
    -> recompute event hash
    -> display VALID
```

Local development should default to a local Hardhat contract or a no-write anchoring mode. Mainnet submission should require an explicit environment setting and user action.

## 24. Docker Deployment Design

The first Fasthosts MVP can run on one Ubuntu VPS with Docker Compose.

```text
Public internet
    |
    v
Caddy: ports 80 and 443
    |
    +--> Web application
    |
    +--> Node API

Private Docker network only
    |
    +--> Python ML service
    +--> Polygon anchoring worker
    +--> PostgreSQL
```

Only Caddy should publish public ports. PostgreSQL, the model service, and the worker must not publish ports to the public interface.

Use persistent volumes for:

- PostgreSQL data;
- the approved model artifact; and
- temporary encrypted backup staging.

Do not bake the `.pkl`, database password, encryption key, or Polygon private key into container images.

## 25. Fasthosts MVP Setup

For a small CPU-based model, start with an Ubuntu VPS with approximately:

```text
6 vCPU
8 GB RAM
240 GB NVMe
```

Increase memory if the model artifact and its Python runtime consume a significant portion of the available RAM.

### Server provisioning

1. Create the VPS in the required UK location.
2. Add an SSH public key.
3. Disable password-based SSH login.
4. Create a non-root deployment user.
5. Apply operating-system updates.
6. Install Docker and the Docker Compose plugin.
7. Configure a firewall.
8. Allow SSH only from trusted administration addresses where possible.
9. Allow public TCP ports `80` and `443`.
10. Do not expose PostgreSQL, the ML port, or worker ports.

### Domain and TLS

1. Register or select the Ibex domain.
2. Point the required DNS `A` record to the VPS IPv4 address.
3. Configure the hostname in the Caddyfile.
4. Start Caddy after DNS has propagated.
5. Confirm HTTPS certificate issuance and renewal.
6. Redirect all HTTP traffic to HTTPS.

### Application deployment

```bash
git clone YOUR_PRIVATE_IBEX_REPOSITORY
cd ibex-platform
cp .env.example .env.production
chmod 600 .env.production
docker compose --env-file .env.production build
docker compose --env-file .env.production run --rm api npm run migrate
docker compose --env-file .env.production up -d
docker compose ps
```

Before public DNS is announced, test health endpoints and the complete synthetic score flow directly through the intended hostname.

### Production secrets

At minimum, protect:

- database password;
- session secret;
- field-encryption key;
- user-hash secret;
- Open Banking credentials for the later pilot;
- expected model artifact hash; and
- Polygon issuer private key.

For the initial MVP, secrets can be stored in a root-owned or deployment-user-owned environment file with restrictive permissions. Move them to a managed secret or signing service before handling a larger real-user pilot.

## 26. Website Requirements

### Public MVP pages

```text
/
/demo
/scores/:scoreEventId
/scores/:scoreEventId/verify
/privacy
/security
/model
```

The first screen should provide the usable demonstration rather than a marketing-only landing page.

### Score screen

Display off-chain:

```text
Ibex score
Score band
Confidence
Positive factors
Negative factors
Model version
Score creation time
```

Display proof metadata:

```text
Score event hash
Merkle root
Model version hash
Issuer address
Polygon timestamp
Transaction hash
Confirmation status
Verification result
```

The interface must make clear that the score and financial data are private, while the proof values are public.

### Verification screen

Verification should:

1. Retrieve the permissioned off-chain event.
2. Canonicalise it using the same proof package.
3. Recompute `scoreEventHash`.
4. Recompute the model commitment.
5. Read the record from Polygon.
6. Verify the Merkle proof when batching is enabled.
7. Compare issuer and contract address with the approved registry.
8. Show `VALID` only when all required checks pass.

## 27. Security Requirements

### Application security

- HTTPS everywhere;
- secure, HTTP-only session cookies;
- CSRF protection for state-changing browser requests;
- rate limiting;
- strict request schemas;
- parameterised database queries;
- least-privilege service accounts;
- no secrets or private financial data in logs;
- dependency and container scanning;
- automated security updates with maintenance controls; and
- tested session revocation.

### Database security

- PostgreSQL accessible only on the private network;
- encryption for sensitive fields;
- encrypted backups;
- unique credentials per environment;
- migration review;
- retention and deletion jobs;
- restoration tests; and
- audit logging for administrative access.

### ML security

- load only the trusted `.pkl`;
- verify the artifact hash before deserialisation;
- pin runtime dependencies;
- validate feature names, types, ranges, and missing values;
- limit request size;
- do not give the ML container the Polygon key;
- do not expose the inference endpoint publicly; and
- preserve model manifests and evaluation records.

### Blockchain security

- separate owner and issuer roles;
- enforce stable user hashes, score periods, cooldowns, duplicate rejection, and issuer quotas;
- keep website-user bans and API throttling off-chain;
- support emergency pausing and rapid issuer revocation;
- keep minimal POL in the server issuer wallet;
- monitor issuer balance;
- validate chain ID and contract bytecode on startup;
- use a backup RPC provider;
- wait for configured confirmations;
- record transaction receipts;
- alert on transaction failure or unexpected issuer changes; and
- keep owner recovery procedures offline.

## 28. Backups, Monitoring, and Recovery

The VPS is a single failure domain. The MVP requires off-site backups even when the public flow uses only synthetic data.

### Backup plan

```text
PostgreSQL logical backup: daily
Encrypted off-site copy: daily
Model artifact and manifest: every approved model release
Infrastructure configuration: every change through Git
Retention: defined and automatically enforced
Restore exercise: before launch and on a regular schedule
```

Use private object storage or the provider's encrypted backup product. Do not treat a second directory on the same VPS as a backup.

### Monitoring plan

Monitor:

- website uptime;
- API response errors and latency;
- ML-service health and active artifact hash;
- PostgreSQL disk usage and connections;
- background-job queue depth;
- Polygon RPC health;
- issuer POL balance;
- transaction submission and confirmation time;
- certificate expiry;
- backup completion; and
- restore-test results.

### Incident basics

Document procedures to:

1. Disable a compromised issuer using the owner account.
2. Rotate API and database credentials.
3. Stop new score creation while preserving reads.
4. Restore PostgreSQL from an off-site backup.
5. compare restored events with Polygon proofs.
6. Communicate affected versions and contract addresses.

## 29. Testing Strategy

### Smart contract tests

- deployer is owner;
- deployer is first approved issuer;
- owner can add and remove issuers;
- non-owner cannot manage issuers;
- approved issuer can submit;
- non-approved issuer cannot submit;
- latest record is correct;
- event is emitted;
- Merkle proof verification works;
- invalid hashes and score periods are rejected;
- repeated periods and too-early updates are rejected;
- reused score-event hashes are rejected;
- daily issuer quotas reset and enforce correctly;
- pause and unpause permissions work;
- removed issuers cannot submit;
- two-step ownership acceptance is enforced; and
- ownership renunciation is disabled.

### Proof tests

- canonical JSON fixture is stable;
- JavaScript and Python fixtures produce the intended shared commitments;
- changed score fails verification;
- changed explanation fails verification;
- changed model manifest fails verification;
- incorrect user salt fails lookup;
- incorrect Merkle proof fails; and
- correct event returns `VALID`.

### Model tests

- expected model artifact hash;
- expected feature schema;
- known input/output regression fixtures;
- missing-feature rejection;
- unexpected-feature rejection;
- type and range validation;
- explanation output schema;
- deterministic behaviour where expected; and
- runtime dependency compatibility.

### API and database tests

- complete score-event transaction;
- encryption and decryption boundaries;
- idempotent anchor job creation;
- recoverable Polygon retry;
- failed transaction state;
- consent withdrawal;
- data deletion and retention;
- unauthorised score access; and
- log redaction.

### End-to-end tests

- synthetic profile to score;
- score to Polygon proof;
- proof read-back;
- verification page;
- tampered database fixture;
- mobile and desktop rendering;
- backup and restore; and
- service restart without lost jobs.

## 30. Release Stages

### Stage 1: Public synthetic-data MVP

Required:

- real approved model artifact;
- synthetic feature profiles;
- no identity or bank onboarding;
- working score and explanation;
- private score-event database;
- intentional Polygon anchoring;
- public verification page;
- backups and monitoring; and
- clear demonstration wording.

### Stage 2: Closed real-user pilot

Additional requirements:

- regulated Open Banking route or appropriately authorised partner;
- explicit consent and revocation;
- privacy notice and retention policy;
- data-protection impact assessment;
- user authentication and access control;
- human review and contestation process;
- model card and evaluation evidence;
- fairness and drift monitoring;
- independent security testing;
- hardened contract and wallet separation; and
- legal and regulatory perimeter review.

### Stage 3: Lender-integrated production

Additional requirements:

- confirmed permissions and contractual roles;
- partner authentication and scoped data access;
- production service-level objectives;
- high-availability application and database design;
- formal model governance and change approval;
- incident reporting and business-continuity procedures;
- periodic access review;
- penetration testing; and
- independent review of score use in consequential decisions.

## 31. Regulatory and User-Safeguard Checkpoints

This section is an engineering checkpoint, not legal advice.

For real Open Banking data, use an appropriately authorised or registered account-information route and obtain explicit user consent. The FCA describes account information services and the relevant registration or authorisation framework:

```text
https://www.fca.org.uk/firms/account-information-services-payment-initiation-services
```

Depending on the final service offered to lenders, landlords, or users, the FCA perimeter for credit-reference or credit-information services may also need review:

```text
https://www.fca.org.uk/firms/authorisation/credit-reference-agencies-information-services
```

For automated scoring using personal data, maintain meaningful information about the model's role, a way to request human intervention, and a way to contest a result. Complete a DPIA before high-risk real-user processing:

```text
https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/artificial-intelligence/explaining-decisions-made-with-artificial-intelligence/part-1-the-basics-of-explaining-ai/legal-framework/
```

The absence of model onboarding data reduces unnecessary collection but does not remove the need to protect financial features, scores, explanations, and inferences.

## 32. MVP Launch Checklist

### Product

- synthetic profiles cover the intended demonstration cases;
- the model uses no onboarding or demographic fields;
- score meaning and limitations are clear;
- explanations are understandable;
- verification is visible; and
- the site does not imply a lending approval.

### Engineering

- all services build from a clean checkout;
- migrations apply to an empty database;
- the model hash matches;
- the full synthetic flow passes;
- contract reads use chain ID `137`;
- mainnet writes require explicit enablement;
- retry handling is tested;
- no sensitive data appears on-chain; and
- no secrets appear in Git or logs.

### Infrastructure

- DNS resolves;
- HTTPS is valid;
- only intended ports are open;
- database and ML services are private;
- backups complete;
- a restore has been tested;
- uptime and wallet alerts work; and
- an incident contact is assigned.

### Blockchain

- approved contract address is configured;
- issuer address is approved;
- issuer has a deliberately limited POL balance;
- PolygonScan links work;
- confirmation policy is configured;
- read-back succeeds; and
- verification returns `VALID` for the known event.

## 33. Team Workstreams

### Frontend

- synthetic profile selector;
- score and explanation dashboard;
- score history;
- proof viewer;
- PolygonScan integration;
- privacy and model information; and
- responsive end-to-end user flow.

### Backend and data

- API contracts;
- database schema and migrations;
- feature pipeline;
- canonical event creation;
- encryption boundaries;
- consent route for the later pilot;
- verification service; and
- operational logs and monitoring.

### ML

- trusted artifact and artifact hash;
- exact feature schema;
- preprocessing package;
- FastAPI inference service;
- explanation output;
- regression fixtures;
- model manifest and model card; and
- evaluation, fairness, and drift plan.

### Blockchain

- contract and proof package;
- issuer operations;
- anchoring worker;
- transaction retry and confirmation;
- PolygonScan verification;
- production contract hardening; and
- wallet separation and incident procedures.

### Infrastructure and security

- Docker Compose;
- Caddy and HTTPS;
- Fasthosts VPS;
- firewall and SSH controls;
- secrets;
- backups;
- monitoring; and
- recovery exercises.

## 34. Updated Pitch Language

> Ibex Credit creates an explainable financial-behaviour rating for internationals who may have little UK credit history. The model uses validated financial features rather than immigration, nationality, address, university, or employer onboarding data. Scores and financial records remain private off-chain, while Polygon stores cryptographic proofs that make each score event independently auditable and tamper-evident.

## 35. Final Architecture Rule

```text
Model inputs:
Only the approved engineered financial features.

Off-chain:
The model, score, explanations, feature values, financial data,
operational accounts, consents, review records, and complete score events.

On-chain:
userHash, scoreEventHash, merkleRoot, modelVersionHash,
contract timestamp, and approved issuer address.
```

This boundary is mandatory. No future feature should put identity, raw financial data, the actual score, or ML inputs on Polygon.
