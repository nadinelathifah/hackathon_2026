"""BUILD 19 -- serve serve/static over HTTP as /static.

The v4 dashboard keeps all of its JavaScript inline, so the app never needed
a static mount and does not have one. That makes
/static/ibex_intervals.js return 404 even though the file exists on disk.

This adds one guarded mount to serve/app.py. Safe to run twice.
Rollback: delete the block marked BUILD 19 STATIC in serve/app.py.

Run once from the project root, then delete this file.
"""
import io
import os
import shutil
import sys

APP = os.path.join('serve', 'app.py')
JS = os.path.join('serve', 'static', 'ibex_intervals.js')
HTML = os.path.join('serve', 'static', 'ibex.html')

BLOCK = '\n'.join([
    '',
    '# BUILD 19 STATIC -- expose serve/static as /static so the score card',
    '# can load ibex_intervals.js. The dashboard HTML is inline otherwise.',
    'try:',
    '    import os as _os_static',
    '    from fastapi.staticfiles import StaticFiles as _StaticFiles',
    '',
    '    _STATIC_DIR = _os_static.path.join(',
    '        _os_static.path.dirname(_os_static.path.abspath(__file__)),',
    "        'static')",
    '    if _os_static.path.isdir(_STATIC_DIR):',
    "        app.mount('/static', _StaticFiles(directory=_STATIC_DIR),",
    "                  name='static')",
    '    else:',
    "        print('static dir not found:', _STATIC_DIR)",
    'except Exception as _static_err:',
    "    print('static mount not added:', _static_err)",
    '',
    '',
])


def main():
    if not os.path.isdir('serve'):
        print('run this from the project root, the folder containing serve/')
        return 1
    if not os.path.isfile(APP):
        print('cannot find ' + APP)
        return 1

    log = []

    if not os.path.isfile(JS):
        print('missing ' + JS)
        print('copy ibex_intervals.js into serve/static/ first')
        return 2
    log.append('found     ' + JS)

    if os.path.isfile(HTML):
        h = io.open(HTML, encoding='utf-8').read()
        if 'ibex_intervals.js' in h:
            log.append('found     script tag already in ibex.html')
        else:
            log.append('WARNING   script tag NOT in ibex.html, run '
                       'install_intervals_js.py as well')

    s = io.open(APP, encoding='utf-8').read()

    if 'BUILD 19 STATIC' in s:
        log.append('skipped   static mount already added')
    elif 'StaticFiles' in s:
        log.append('WARNING   app.py already imports StaticFiles somewhere,')
        log.append('          not touching it -- tell me and we will look')
    else:
        dst = APP + '.static.bak'
        if not os.path.exists(dst):
            shutil.copyfile(APP, dst)
            log.append('backup    ' + dst)
        i = s.find('if __name__ ==')
        if i >= 0:
            s = s[:i] + BLOCK.lstrip('\n') + '\n' + s[i:]
            log.append('patched   mount added above the __main__ guard')
        else:
            if not s.endswith('\n'):
                s = s + '\n'
            s = s + BLOCK
            log.append('patched   mount added at the end of app.py')
        io.open(APP, 'w', encoding='utf-8', newline='').write(s)

    for line in log:
        print('  ' + line)
    print('')
    print('done. next:')
    print('  py -3.13 -m py_compile serve/app.py')
    print('  restart uvicorn, then this must return 200:')
    print('  Invoke-WebRequest "http://localhost:8000/static/'
          'ibex_intervals.js" | Select-Object StatusCode')
    return 0


if __name__ == '__main__':
    sys.exit(main())
