#!/usr/bin/env python3
"""BUILD 19 -- fetch the bank-verified account-holder name from TrueLayer /info.

Why this exists
---------------
serve/ibex_v3.py::_holder_name_for() already looks for a holder name and
serve/ibex_v4.py::score_run() already calls it, but obcredit/truelayer/client.py
never calls GET /data/v1/info, so there is nothing to find. The `info` scope is
already requested at connect time, so no consent change is needed.

What it does
------------
1. inserts TrueLayerClient.fetch_info() and ._holder_from_info() before _safe_json
2. adds "info", "holder_name" and "full_name" to the dict returned by fetch_user

Idempotent. Backup: obcredit/truelayer/client.py.info.bak
"""
from __future__ import annotations

import os
import shutil
import sys

CLIENT = os.path.join("obcredit", "truelayer", "client.py")
MARKER = "BUILD 19 INFO"

ANCHOR_METHODS = "    def _safe_json(self, path: str)"

ANCHOR_RETURN = '        return {\n            "case_id": case_id,'

METHODS = '''    # ------------------------------------------------ identity  BUILD 19 INFO
    def fetch_info(self):
        """GET /info -- the bank-verified identity block.

        Needs the `info` scope, which is already requested at connect time.
        Tolerant: some providers do not expose /info at all.
        """
        try:
            rows = self._safe_json("/info")
        except Exception as exc:            # pragma: no cover
            log.debug("/info unavailable: %s", exc)
            return []
        return rows or []

    @staticmethod
    def _holder_from_info(rows):
        """Pull the first usable holder name out of an /info response."""
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in ("full_name", "name", "account_holder_name"):
                val = row.get(key)
                if isinstance(val, str) and val.strip():
                    return " ".join(val.split())
            names = row.get("names")
            if isinstance(names, list):
                for nm in names:
                    if isinstance(nm, str) and nm.strip():
                        return " ".join(nm.split())
                    if isinstance(nm, dict):
                        val = nm.get("full_name") or nm.get("name")
                        if isinstance(val, str) and val.strip():
                            return " ".join(val.split())
        return ""

'''

RETURN_NEW = '''        _info_rows = self.fetch_info()
        _holder = self._holder_from_info(_info_rows)
        if _holder:
            log.info("account holder from /info: %s", _holder)
        else:
            log.warning("no account-holder name returned by /info; the score "
                        "handle will fall back to the typed email alone")
        return {
            "info": _info_rows,
            "holder_name": _holder,
            "full_name": _holder,
            "case_id": case_id,'''


def main() -> int:
    if not os.path.isfile(CLIENT):
        print("NOT FOUND:", CLIENT)
        print("Run this from the folder that contains obcredit/ and serve/.")
        return 2

    src = open(CLIENT, encoding="utf-8").read()

    if MARKER in src:
        print("already installed -- nothing to do")
        return 0

    if "def fetch_info(" in src:
        print("WARNING: client.py already defines fetch_info() but has no")
        print("BUILD 19 INFO marker. Refusing to patch. Inspect it by hand.")
        return 3

    if ANCHOR_METHODS not in src:
        print("anchor not found: _safe_json definition")
        return 4
    if ANCHOR_RETURN not in src:
        print("anchor not found: fetch_user return dict")
        return 5
    if src.count(ANCHOR_RETURN) != 1:
        print("return anchor is not unique (%d matches)" % src.count(ANCHOR_RETURN))
        return 6

    shutil.copyfile(CLIENT, CLIENT + ".info.bak")

    out = src.replace(ANCHOR_METHODS, METHODS + ANCHOR_METHODS, 1)
    print('inserted fetch_info() and _holder_from_info()')

    out = out.replace(ANCHOR_RETURN, RETURN_NEW, 1)
    print('added "info", "holder_name", "full_name" to the fetch_user payload')

    open(CLIENT, "w", encoding="utf-8").write(out)
    print("backup", CLIENT + ".info.bak")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
