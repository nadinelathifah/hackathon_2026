"""Pull a TrueLayer sandbox (mock-bank) user and build the SAME features.

Prerequisites (see README section 'Using the TrueLayer mock account'):
  export TL_CLIENT_ID=sandbox-...   TL_CLIENT_SECRET=...
  export TL_ACCESS_TOKEN=...        # obtained via the auth redirect once

    python scripts/run_truelayer.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcredit.adapters import TrueLayerAdapter  # noqa: E402
from obcredit.pipeline import FeaturePipeline  # noqa: E402
from obcredit.truelayer import TrueLayerDataClient  # noqa: E402


def main():
    client = TrueLayerDataClient(
        client_id=os.environ["TL_CLIENT_ID"],
        client_secret=os.environ["TL_CLIENT_SECRET"],
        sandbox=True,
        access_token=os.environ.get("TL_ACCESS_TOKEN"),
    )
    payload = client.fetch_user(case_id=os.environ.get("TL_CASE_ID", "sandbox-user"))
    matrix = FeaturePipeline().build_matrix(TrueLayerAdapter([payload]).to_canonical())
    print(matrix.T)


if __name__ == "__main__":
    main()
