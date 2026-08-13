#!/usr/bin/env python3
r"""
patch_hero_bg.py -- gradient wash behind section 1 of the landing page.

    py -3.13 scripts\patch_hero_bg.py --diag     # what is on the page now
    py -3.13 scripts\patch_hero_bg.py --list     # numbered blocks
    py -3.13 scripts\patch_hero_bg.py            # apply (pure CSS)
    py -3.13 scripts\patch_hero_bg.py --image    # apply using hero-bg.jpg
    py -3.13 scripts\patch_hero_bg.py --index 2  # target a different block
    py -3.13 scripts\patch_hero_bg.py --undo     # revert

Default is a pure-CSS gradient: nothing to serve, nothing to 404, and it
scales to any viewport width.
"""
import os
import re
import shutil
import sys

MARK = "ibex-hero-bg"
PAGE = os.path.join("serve", "web", "index.html")
DEST_IMG = os.path.join("serve", "static", "hero-bg.jpg")
BAK = PAGE + ".herobg.bak"

GRAD = ("radial-gradient(ellipse 62% 74% at 88% 6%, rgba(208,255,113,.60) 0%, rgba(208,255,113,0) 62%),"
        "radial-gradient(ellipse 46% 58% at 79% 52%, rgba(95,169,140,.26) 0%, rgba(95,169,140,0) 66%),"
        "radial-gradient(ellipse 54% 64% at 4% 94%, rgba(76,126,255,.20) 0%, rgba(76,126,255,0) 62%),"
        "radial-gradient(ellipse 40% 50% at 26% 8%, rgba(212,238,228,.45) 0%, rgba(212,238,228,0) 70%)")


def css(use_image):
    layer = 'url("/static/hero-bg.jpg")' if use_image else GRAD
    size = "cover" if use_image else "auto"
    return ('<style id="ibex-hero-bg-css">\n.' + MARK + '{\n'
            '  background-color:#FFFFFF !important;\n'
            '  background-image:' + layer + ' !important;\n'
            '  background-repeat:no-repeat !important;\n'
            '  background-position:center center !important;\n'
            '  background-size:' + size + ' !important;\n'
            '  position:relative !important;\n}\n'
            '.' + MARK + ' > div:not([class*="card"]):not([class*="Card"]){background-color:transparent !important;}\n'
            '@media (max-width:820px){.' + MARK + '{background-position:70% center !important;}}\n'
            '</style>\n')


CLASS_RE = re.compile(r'<(?!style\b)[a-z]+[^>]*\bclass="[^"]*' + MARK + r'[^"]*"[^>]*>', re.I)


def applied(html):
    return CLASS_RE.search(html)


def candidates(html):
    b = html.lower().find("<body")
    start = b if b >= 0 else 0
    out = []
    for m in re.finditer(r"<(section|header|main|div)\b[^>]*>", html[start:], re.I):
        tag = m.group(0)
        name = m.group(1).lower()
        if name == "div" and "hero" not in tag.lower():
            continue
        pos = start + m.start()
        seg = html[pos:pos + 1500]
        txt = re.sub(r"<[^>]+>", " ", seg)
        txt = re.sub(r"\s+", " ", txt).strip()[:80]
        out.append((pos, pos + len(tag), tag, txt, name))
    return out


def default_index(cands):
    """First real band in document order. <main> is a wrapper, never the hero."""
    for i, c in enumerate(cands):
        if c[4] != "main":
            return i
    return 0


def show(cands):
    print("blocks found in <body>:")
    for i, c in enumerate(cands):
        print("  [%d] %-54s | %s" % (i, c[2][:54], c[3][:56]))
    if cands:
        print("")
        print("default would be [%d]" % default_index(cands))


def diag(html):
    print("file            : %s" % PAGE)
    print("class applied   : %s" % ("YES" if applied(html) else "NO"))
    print("css block       : %s" % ("YES" if "ibex-hero-bg-css" in html else "NO"))
    print("image on disk   : %s" % ("YES" if os.path.exists(DEST_IMG) else "no (not needed for CSS mode)"))
    print("backup exists   : %s" % ("YES" if os.path.exists(BAK) else "NO"))
    m = applied(html)
    print("tagged element  : %s" % (m.group(0)[:90] if m else "none"))
    print("")
    show(candidates(html))


def add_class(tag):
    for q in ('class="', "class='"):
        p = tag.find(q)
        if p >= 0:
            cut = p + len(q)
            return tag[:cut] + MARK + " " + tag[cut:]
    return tag[:-1].rstrip() + ' class="' + MARK + '">'


def main(argv):
    if not os.path.exists(PAGE):
        print("ERROR %s not found -- run from the repo root" % PAGE)
        return 1
    html = open(PAGE, encoding="utf-8").read()

    if "--undo" in argv:
        if os.path.exists(BAK):
            shutil.copyfile(BAK, PAGE)
            print("OK    reverted from %s" % BAK)
            return 0
        print("ERROR no backup at %s" % BAK)
        return 1
    if "--diag" in argv:
        diag(html)
        return 0
    cands = candidates(html)
    if "--list" in argv:
        show(cands)
        return 0

    use_image = "--image" in argv
    if use_image:
        here = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(here, "hero-bg.jpg")
        if os.path.exists(src) and os.path.isdir(os.path.dirname(DEST_IMG)):
            shutil.copyfile(src, DEST_IMG)
            print("OK    image -> %s (%d bytes)" % (DEST_IMG, os.path.getsize(DEST_IMG)))
        else:
            print("ERROR --image needs hero-bg.jpg next to this script")
            return 1

    if applied(html):
        print("OK    already applied. --undo first if you want to change it.")
        return 0
    if not cands:
        print("ERROR no <section>/<header> found in %s" % PAGE)
        return 1

    idx = default_index(cands)
    if "--index" in argv:
        try:
            idx = int(argv[argv.index("--index") + 1])
        except Exception:
            print("ERROR --index needs a number")
            return 1
    if idx < 0 or idx >= len(cands):
        print("ERROR index %d out of range (0..%d)" % (idx, len(cands) - 1))
        show(cands)
        return 1

    s, e, tag, txt, _ = cands[idx]
    if not os.path.exists(BAK):
        open(BAK, "w", encoding="utf-8").write(html)
    new_tag = add_class(tag)
    html = html[:s] + new_tag + html[e:]
    low = html.lower()
    h = low.find("</head>")
    block = css(use_image)
    if h < 0:
        p = html.find(new_tag)
        html = html[:p] + block + html[p:]
        print("OK    css injected inline (no </head>)")
    else:
        html = html[:h] + block + html[h:]
        print("OK    css injected before </head>")
    open(PAGE, "w", encoding="utf-8").write(html)
    print("OK    mode: %s" % ("image" if use_image else "pure CSS gradient"))
    print("OK    tagged block [%d]: %s" % (idx, tag[:66]))
    print("      content starts: %s" % txt[:66])
    print("")
    print("Not the right band?  --undo  then  --list  then  --index N")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
