# Ibex Credit — all-in-one image: Python 3.12 app + Node 20 chain tooling.
# Render auto-detects this file and builds a Docker service from it.
FROM python:3.12-slim

# Node.js 20 LTS (Hardhat rejects newer majors)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cached layer)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Chain project: install dependencies and compile the contract at build time
COPY chain/package.json ./chain/package.json
RUN cd /app/chain && npm install --no-audit --no-fund
COPY chain/ ./chain/
RUN cd /app/chain && npx hardhat compile

# The app itself
COPY . .

# Non-secret config is baked into the image. Secrets (PRIVATE_KEY, USER_SALT,
# TRUELAYER_CLIENT_SECRET) are Render dashboard env vars injected at runtime;
# the chain scripts pick them up because dotenv never overrides existing vars.
ENV IBEX_ARTIFACTS=artifacts_v5 \
    OBCREDIT_ARTIFACTS=artifacts_v5 \
    IBEX_CHAIN_DIR=/app/chain \
    IBEX_CHAIN_NETWORK=polygon \
    SCORE_EVENT_FILE=./score-event.json \
    PYTHONUNBUFFERED=1

EXPOSE 10000
CMD ["sh", "-c", "uvicorn serve.app:app --host 0.0.0.0 --port ${PORT:-10000}"]
