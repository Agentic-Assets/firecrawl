# Current held-stack baseline (AGENTIC-3752)

`cre_current_stack.py` previews by default. Explicit `--observe`, with the exact
`--tooling-sha` and `--tooling-tree`, requires a clean committed tooling checkout
and verifies the executing producer bytes against that commit. It reads the pinned
native Docker/Compose project and the prior BBC image build evidence, then writes
an exclusive private ext4 model. It does not start containers or tasks. Environment
values and mount sources remain in memory; the model contains private per-field
hashes, five normalized container identities, source bindings and current volume
metadata. The observed model is a proposal until separately reviewed.

The review binds the exact model and prior failed release, acknowledges that the
previous NuQ anonymous-volume identity is unknown, and cannot assert historical
data continuity. The model also exposes the resolved dependency graph and the
current generated Compose dependency label as hashes and counts; the review must
acknowledge that metadata explicitly, including any `--no-deps` divergence.
Retained validation establishes equality to that reviewed
commitment; private values cannot be reconstructed from hashes. Live validation
separately re-observes the actual stack and requires exact model equality. The
original failed operation and its recovery evidence remain distinct. This model
does not authorize a collector run or establish a database outcome.
