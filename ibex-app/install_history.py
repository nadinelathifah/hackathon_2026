"""Install serve/history.py over the legacy inline history helpers.

Why a patcher rather than just adding an import
----------------------------------------------
app.py (and possibly ibex_v4.py) define their own realised_history and
count_credit_accounts. A plain import at the top of the file would be shadowed
by those later definitions, so nothing would change. This script RENAMES the
legacy definitions and then injects the import, so the new module wins no
matter where it sits in the file.

Run from the project root:

    py -3.13 install_history.py

Every touched file is backed up to NAME.histfix.bak first. Nothing is deleted,
so the legacy code stays readable under its new _legacy_ prefix.
"""
import io
import os
import re
import shutil

TARGETS = [
    os.path.join("serve", "app.py"),
    os.path.join("serve", "ibex_v4.py"),
    os.path.join("serve", "ibex_v3.py"),
]

NAMES = ("realised_history", "count_credit_accounts")
IMPORT_LINE = (
    "from serve.history import realised_history, count_credit_accounts"
    "  # BUILD 19 history fix"
)


def inject_import(text):
    """Place the import after the last top-level import in the file header."""
    if "from serve.history import" in text:
        return text, False
    lines = text.split("\n")
    last = -1
    for i, line in enumerate(lines[:120]):
        if re.match(r"^(import|from)\s+\S", line):
            last = i
    if last < 0:
        return IMPORT_LINE + "\n" + text, True
    lines.insert(last + 1, IMPORT_LINE)
    return "\n".join(lines), True


def main():
    if not os.path.isdir("serve"):
        print("ERROR: run this from the project root, the folder holding serve/")
        return 1
    if not os.path.isfile(os.path.join("serve", "history.py")):
        print("ERROR: serve/history.py is missing -- copy it in first")
        return 1

    touched = 0
    for path in TARGETS:
        if not os.path.isfile(path):
            print("%-22s skipped, not present" % path)
            continue

        original = io.open(path, encoding="utf-8").read()
        text = original
        renamed = {}

        for name in NAMES:
            pattern = r"\bdef\s+" + name + r"\s*\("
            hits = len(re.findall(pattern, text))
            if hits:
                text = re.sub(pattern, "def _legacy_" + name + "(", text)
                renamed[name] = hits

        text, added = inject_import(text)

        if text == original:
            print("%-22s no change needed" % path)
            continue

        shutil.copyfile(path, path + ".histfix.bak")
        io.open(path, "w", encoding="utf-8", newline="").write(text)
        touched += 1
        print(
            "%-22s renamed %s ; import %s ; backup written"
            % (path, renamed if renamed else "none", "added" if added else "present")
        )

    print("")
    print("%d file(s) changed" % touched)
    print("Now run: py -3.13 -m py_compile serve/app.py serve/ibex_v4.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
