"""Check an *installed* agentdynamics has every file the console loads. Run from outside the checkout.

Package-data globs don't follow subfolders: web/* ships web/app.js but not web/pages/monitor.js. A page
script missing from the wheel breaks the console for everyone who pip-installs, while every test run from
the source tree passes. So this reads index.html and checks each <script src> it loads is really there.
"""
import os
import re
import sys

import agentdynamics

here = os.path.dirname(agentdynamics.__file__)
if "site-packages" not in here:
    sys.exit(f"imported from {here}, not an installed package -- run this outside the checkout")
html = open(os.path.join(here, "web", "index.html"), encoding="utf-8").read()
scripts = re.findall(r'<script src="/([^"]+)"', html)
need = [f"web/{s}" for s in scripts] + ["web/index.html", "web/style.css", "bootstrap/sitecustomize.py"]
missing = [p for p in need if not os.path.exists(os.path.join(here, p))]
if len(scripts) < 3 or missing:
    sys.exit(f"missing from the installed package: {missing or 'index.html loads fewer scripts than expected'}")
print(f"agentdynamics {agentdynamics.__version__}: all {len(need)} console files present")
