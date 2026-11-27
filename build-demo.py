#!/usr/bin/env python3
"""
build-demo.py - keep the three copies of the engine in step.

1. Renders every named sound with ZeroSounds4.py at the default seed and writes
   the CRC-32 table into ZeroSounds4.jsx (the ZS_ORACLE constant).
2. Inlines ZeroSounds4.jsx into ZeroSounds4-demo.html between the
   COMPONENT markers, so the demo opens straight from disk with no server.

Run it after any change to ZeroSounds4.py or ZeroSounds4.jsx:

    python build-demo.py
"""
import json
import os
import re

import ZeroSounds4 as zs

HERE = os.path.dirname(os.path.abspath(__file__))
JSX = os.path.join(HERE, 'ZeroSounds4.jsx')
DEMO = os.path.join(HERE, 'ZeroSounds4-demo.html')


def main():
    oracle = {k: '%08X' % zs.zs_checksum(zs.zs_render(zs.zb_derive_params(k, zs.DEFAULT_SEED)))
              for k in zs.ZB_SOUNDS}
    jsx = open(JSX, encoding='utf-8').read()
    jsx, n = re.subn(r'/\*ORACLE\*/.*?/\*END\*/', lambda m: '/*ORACLE*/' + json.dumps(oracle) + '/*END*/',
                     jsx, count=1, flags=re.S)
    assert n == 1, 'ZS_ORACLE marker not found in ZeroSounds4.jsx'
    open(JSX, 'w', encoding='utf-8').write(jsx)

    # Browser build: React comes from the page's global, and there is no module system.
    body = re.sub(r"^import React.*?;\n", "const { useState, useRef, useCallback, useEffect, useMemo } = React;\n",
                  jsx, count=1, flags=re.M)
    body = re.sub(r"\nexport \{.*?\};\n", "\n", body, count=1, flags=re.S)
    body = body.replace("export default ZeroSounds4;\n", "")
    assert 'import ' not in body.split('// <ENGINE>')[0] and 'export ' not in body

    html = open(DEMO, encoding='utf-8').read()
    html, n = re.subn(r'(// <COMPONENT>\n).*?(// </COMPONENT>)', lambda m: m.group(1) + body + m.group(2),
                      html, count=1, flags=re.S)
    assert n == 1, 'COMPONENT markers not found in ZeroSounds4-demo.html'
    open(DEMO, 'w', encoding='utf-8').write(html)
    print('oracle: %d checksums written to ZeroSounds4.jsx' % len(oracle))
    print('demo:   ZeroSounds4.jsx inlined into ZeroSounds4-demo.html (%d bytes)' % len(html))


if __name__ == '__main__':
    main()
