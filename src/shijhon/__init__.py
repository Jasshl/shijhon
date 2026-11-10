"""Shijhon: blend your own music library with a music catalog, for Subsonic clients.

Copyright (C) 2026 Jasshl. Licensed under the GNU Affero General Public License, version 3
or (at your option) any later version: see the files LICENSE and NOTICE.
"""

import importlib.util
import sys

# httpcore imports sniffio for every lock it sets up and every connection it closes. That
# optional package is not installed, and a failed import searches the path again each time:
# a fifth of a request's work. An entry of None answers the import at once, as failed.
if importlib.util.find_spec("sniffio") is None:
    sys.modules["sniffio"] = None  # type: ignore[assignment]

__version__ = "0.6.0"
# The author attribution and the license the dashboard's footer shows on every page, with a
# link to the source (``delivery.netpolicy.PROJECT_URL``). They are the program's legal
# notices: a modified version keeps them (NOTICE, the additional term under section 7(b)).
__copyright__ = "\u00a9 2026 Jasshl"
__license__ = "AGPL-3.0-or-later"
