#!/usr/bin/env python3
"""make_business_page.py -- build serve/static/business.html.

Run from the step3 repo root:
    py -3.13 scripts/make_business_page.py

The head (design tokens, fonts, nav and component CSS) is lifted verbatim from
serve/static/blockchain-proof.html, so the nav bar, logo, buttons, cards and
colours are identical to the rest of the site. Only the page-specific rules in
scripts/business_extra.css are added on top.
"""
from __future__ import annotations
import os
import re
import sys

SRC = os.path.join("serve", "static", "blockchain-proof.html")
OUT = os.path.join("serve", "static", "business.html")
CSS = os.path.join("scripts", "business_extra.css")
BODY = os.path.join("scripts", "business_body.html")
JS = os.path.join("scripts", "business_page.js")


def read(p):
    if not os.path.exists(p):
        sys.exit("FATAL: missing " + p + " (run from the repo root)")
    return open(p, encoding="utf-8").read()


def main():
    src = read(SRC)
    marker = "</style>"
    if marker not in src:
        sys.exit("FATAL: no </style> in " + SRC + " -- cannot reuse its head")
    head = src[:src.index(marker)]
    head = re.sub(r"<title>.*?</title>",
                  "<title>For businesses | Ibex Credit</title>",
                  head, count=1, flags=re.S)

    body = read(BODY).replace(
        "</body>", "<script>\n" + read(JS) + "\n</script>\n</body>")

    out = (head + "\n/* --- for-businesses page --- */\n" + read(CSS)
           + marker + "\n</head>\n" + body)

    open(OUT, "w", encoding="utf-8", newline="\n").write(out)
    print("[make_business_page] wrote %s (%d bytes)" % (OUT, len(out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
