"""GC Workspace -- standalone GC/GC-MS integration, identification and reporting."""

__version__ = "0.1.0"

#: Bumped whenever the integrator can produce different peaks from the same
#: input; stored in project files so a changed result is reported, not hidden.
INTEGRATOR_VERSION = 1

from gcws.paths import bootstrap_legacy as _bootstrap_legacy

_bootstrap_legacy()
