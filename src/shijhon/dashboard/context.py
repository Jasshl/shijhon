"""Shared context for the dashboard and standalone pages."""

from shijhon import __copyright__, __license__, __version__
from shijhon.delivery.netpolicy import PROJECT_URL


def legal_context() -> dict[str, str]:
    """The licence and source attribution shown in every page's footer."""
    return {
        "version": __version__,
        "copyright": __copyright__,
        "license": __license__,
        "source_url": PROJECT_URL,
    }
