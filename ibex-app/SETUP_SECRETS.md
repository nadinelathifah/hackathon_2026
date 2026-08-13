# Ibex Credit — setup & secrets

This repository contains **no secrets**. Everything private is created locally
or entered in the Render dashboard. This file is the checklist.

---

## 1. Files the app creates itself (gitignored — do not commit)

| File | Created when | Contains |
|---|---|---|
| `serve/users.json` | first signup | emails + PBKDF2 password hashes |
| `serve/_anchors/*.json` | each on-chain anchor | the anchored score events |
| `chain/score-event.json` | each score (local hardhat flow) | the latest score event preimage |

If any of these are already tracked by git, untrack them once:

```powershell
git rm --cached serve/users.json score-event.json
git log --all -- .env serve/users.json    # should print nothing
```

## 2. Local development (PowerShell, from the repo root)

```powershell
$env:IBEX_ARTIFACTS="artifacts_v5"
$env:OBCREDIT_ARTIFACTS="artifacts_v5"
$env:IBEX_OB_MATRIX="C:\Users\Josep\Downloads\obcache_b22\ob_matrix_full_all.pkl"
$env:TRUELAYER_CLIENT_ID="sandbox-ibexcredit-132352"
$env:TRUELAYER_CLIENT_SECRET="<your sandbox client secret>"
$env:TRUELAYER_REDIRECT_URI="http://localhost:8000/callback"
$env:TRUELAYER_SANDBOX="1"
$env:IBEX_CHAIN_NETWORK="polygon"
$env:IBEX_CHAIN_DIR="C:\Users\Josep\Downloads\hackathon_2026\ibex-smart-contract-demo-v2"
$env:IBEX_SCORE_EVENT_PATH="C:\Users\Josep\Downloads\hackathon_2026\ibex-smart-contract-demo-v2\score-event.json"
py -3.13 -m uvicorn serve.app:app --port 8000
```

Locally the hardhat scripts in `chain/` (or your standalone clone) do the
anchoring, unchanged. If `PRIVATE_KEY` is also set in the server environment
the server prefers its built-in direct-RPC path instead — same contract,
same hashes, no node.

## 3. Chain configuration

Locally, the chain project reads `chain/.env` (or the standalone clone's):

```
PRIVATE_KEY=<issuer wallet private key>
ISSUER_ADDRESS=0x4bCa26d44634966C75abdBCec41DDf94a930a49c
SCORE_EVENT_FILE=./score-event.json
USER_SALT=<64-hex salt — never rotate mid-demo; every anchor depends on it>
POLYGON_RPC_URL=https://polygon-bor-rpc.publicnode.com
SCORE_AUDIT_V2_CONTRACT_ADDRESS=0x8621D09F08C2f58803e7239F8D46D444e0eF63e1
POLYGON_EXPLORER_BASE_URL=https://polygonscan.com
```

## 4. Render — the plain Python service is enough

`serve/chain_rpc.py` anchors and verifies **directly over JSON-RPC** — a
pure-Python client (RFC 6979 signing, RLP, ABI) validated against the
published EIP-155 test vector. No Node runtime, no Docker, no hardhat
subprocess. Your existing Python web service gets full chain support just
by deploying this code.

(A `Dockerfile` and the vendored `chain/` project are still included and
also work — they're simply no longer required for chain functionality.)

Set these in the service's **Environment** tab:

| Variable | Value | Notes |
|---|---|---|
| `IBEX_ARTIFACTS` | `artifacts_v5` | without it the service silently serves the old model |
| `OBCREDIT_ARTIFACTS` | `artifacts_v5` | same |
| `TRUELAYER_CLIENT_ID` | `sandbox-ibexcredit-132352` | |
| `TRUELAYER_CLIENT_SECRET` | the real sandbox secret | secret |
| `TRUELAYER_REDIRECT_URI` | `https://<service>.onrender.com/callback` | must also be registered in the TrueLayer console |
| `TRUELAYER_SANDBOX` | `1` | |
| `IBEX_CHAIN_NETWORK` | `polygon` | drives explorer links on the score page |
| `POLYGON_RPC_URL` | `https://polygon-bor-rpc.publicnode.com` | public fallbacks are built in |
| `SCORE_AUDIT_V2_CONTRACT_ADDRESS` | `0x8621D09F08C2f58803e7239F8D46D444e0eF63e1` | |
| `PRIVATE_KEY` | the issuer wallet key | secret — used only to sign anchors |
| `USER_SALT` | the same 64-hex salt as every anchor so far | secret; **do not rotate** — a different salt makes every existing anchor unreachable |

Leave unset on Render: `IBEX_CHAIN_DIR`, `IBEX_SCORE_EVENT_PATH`,
`IBEX_OB_MATRIX` (its absence is what makes the admin cohort fall back to
the bundled 100-row sample).

## 5. The admin cohort on Render (512 MB)

The full OB matrix is gigabytes and is **not** in this repo. The admin cohort
falls back to `fixtures/ob_matrix_admin100.pkl` — 100 rows sampled from the
evaluation slice, small enough to commit — whenever the full matrix is absent.

Regenerate it (once, on the research machine, with `IBEX_OB_MATRIX` set):

```powershell
py -3.13 scripts\make_admin_sample.py
git add fixtures/ob_matrix_admin100.pkl
```

## 6. Known deployment limitation

Render's filesystem is ephemeral: redeploys wipe `serve/users.json` and
`serve/_anchors/`. Accounts re-register in seconds and the chain records are
untouched; the business page's file-based and name+email verification keep
working regardless, because they recompute and read the chain directly.
Transaction-id lookup of pre-redeploy anchors is the one thing that needs
the same disk. The production fix is a persistent disk or managed database.
