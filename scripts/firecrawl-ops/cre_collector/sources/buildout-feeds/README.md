Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Buildout feed coordinates

The existing 25 registered brokerage tuples and four core SVN, Lee and Franklin
feed tuples form one read-only registry. Franklin's sale and lease feeds have
distinct feed keys under the same source key. The helper validates tuple shapes,
known source keys, clean HTTPS listing hosts and unique feed and plugin keys.
It does not fetch providers, change pacing or qualify inventory; all transport,
freshness and source policies remain with their existing owners.

This consolidation and its private-capture integration remain unverified under
the operator's test and service hold. No live source access or deployment follows
from the registry entries.
