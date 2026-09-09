"""Check the pre-health-endpoint application during the one-time bootstrap."""
from release import ready

if not ready('legacy-bootstrap'):
    raise SystemExit('Legacy login endpoint is not ready')
