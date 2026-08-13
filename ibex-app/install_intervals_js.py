"""BUILD 19 -- wire the per-score interval into the v4 score card.

Three steps, each safe to run twice:

  1. check serve/static/ibex_intervals.js is in place
  2. add one script tag to serve/static/ibex.html before the closing body tag
  3. attach the interval router from serve/intervals.py to the FastAPI app,
     because the JS is useless without those two endpoints

Run once from the project root, then delete this file.
"""
import io
import os
import shutil
import sys

HTML = os.path.join('serve', 'static', 'ibex.html')
APP = os.path.join('serve', 'app.py')
JS = os.path.join('serve', 'static', 'ibex_intervals.js')

TAG = '<script src="/static/ibex_intervals.js"></script>'
CLOSE = '</body>'

ROUTER = '\n'.join([
    '',
    '# BUILD 19 -- per-block Wilson interval endpoints used by the score card',
    'try:',
    '    from serve.intervals import build_router as _build_interval_router',
    '',
    '    app.include_router(_build_interval_router())',
    'except Exception as _interval_err:',
    "    print('interval router not loaded:', _interval_err)",
    '',
    '',
])


def backup(path, suffix):
    dst = path + suffix
    if os.path.exists(dst):
        return 'backup    ' + dst + ' (kept existing)'
    shutil.copyfile(path, dst)
    return 'backup    ' + dst


def do_html(log):
    if not os.path.isfile(HTML):
        return 'cannot find ' + HTML
    s = io.open(HTML, encoding='utf-8').read()
    if TAG in s:
        log.append('skipped   script tag already present in ibex.html')
        return None
    n = s.count(CLOSE)
    if n != 1:
        return ('expected exactly one closing body tag in ' + HTML
                + ', found ' + str(n) + ' -- nothing written')
    log.append(backup(HTML, '.intervals.bak'))
    s = s.replace(CLOSE, '  ' + TAG + '\n' + CLOSE)
    io.open(HTML, 'w', encoding='utf-8', newline='').write(s)
    log.append('patched   script tag added to ibex.html')
    return None


def do_app(log):
    if not os.path.isfile(APP):
        return 'cannot find ' + APP
    s = io.open(APP, encoding='utf-8').read()
    if 'serve.intervals' in s:
        log.append('skipped   interval router already attached in app.py')
        return None
    log.append(backup(APP, '.intervals.bak'))
    i = s.find('if __name__ ==')
    if i >= 0:
        s = s[:i] + ROUTER.lstrip('\n') + '\n' + s[i:]
        log.append('patched   router attached above the __main__ guard')
    else:
        if not s.endswith('\n'):
            s = s + '\n'
        s = s + ROUTER
        log.append('patched   router attached at the end of app.py')
    io.open(APP, 'w', encoding='utf-8', newline='').write(s)
    return None


def main():
    if not os.path.isdir('serve'):
        print('run this from the project root, the folder containing serve/')
        return 1
    log = []
    if not os.path.isfile(JS):
        print('missing ' + JS)
        print('')
        print('copy it out of the ibex_wire zip first, then run this again')
        return 2
    log.append('found     ' + JS)
    for step in (do_html, do_app):
        err = step(log)
        if err:
            print(err)
            for line in log:
                print('  ' + line)
            return 3
    for line in log:
        print('  ' + line)
    print('')
    print('done. next:')
    print('  py -3.13 -m py_compile serve/app.py')
    print('  restart uvicorn, then hit /api/v4/interval?pd=0.045')
    return 0


if __name__ == '__main__':
    sys.exit(main())
