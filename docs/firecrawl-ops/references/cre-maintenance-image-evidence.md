Part of [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752).

# Prepared image evidence

`cre_runtime_maintenance_build.py` prepares uniquely tagged images without
recreating a service. Its default mode prints a preview; `--prepare` is the
explicit build operation. The build specification names the exact selected
commit and tree in `new`, a separate top-level `build_source` object containing
its clean standalone ext4 `root` and exact selected `identity`, the native
Windows development and project checkouts and ext4 runtime in `roots`,
the verified native Git and Docker programs and Docker executable SHA-256,
and a unique `image_tags` mapping for the selected source-built services.
Build output must be a new ext4 directory outside the source checkout.

The producer verifies the Git archive against every tracked blob and executable
bit, creates an immutable Docker context tar, and streams those exact bytes to
the native Docker CLI using the Desktop Linux engine. It records the exact
command, context, separate closed stdout and stderr, successful exit, clocks,
image ID and selected revision label in `prepared-images.json`. It rechecks the
source checkout and archive after building. The receipt is an input for review;
it is not authorization to change a running container or schedule.

After the image IDs are selected, `--observe-config` uses a separate, new
Windows-mapped output directory outside the runtime checkout. Its specification
also names the two original native Compose files, `expected_images`, and the
reviewed `allowed_image_default_changes` key sets. It reads the actual native
five-container project, current and prepared image metadata, and resolved
Compose configuration in memory. It emits only a normalized before snapshot,
derived expected configuration hashes, source fingerprints and a small Compose
overlay containing selected image tags and the existing `unless-stopped` restart
policy. Raw environment values and mount sources are not emitted.

The projection refuses a changed Compose creation hash, pending explicit
environment or label changes, unsupported mutable resource settings, unreviewed
image defaults, configuration differences outside the selected images and
restart policy, or a fleet that changes during observation. A later independent
fresh projection must match the reviewed `container_config_before`,
`expected_after_config`, `compose_sources`, and `compose_override` evidence.
The prepared image IDs and tags must be rechecked at use. Observation and
prepared evidence do not replace the canonical lease, source settlement,
Scheduler history, independent readback, or restoration gates.

The top-level `native_compose` binding contains the Docker-discovered plugin
`path`, its `sha256`, and the exact `aliases` set. Docker Desktop's installed
plugin may have multiple NTFS hardlinks; the verifier admits them only when all
links are explicitly listed, resolve to the same file and remain under the
Docker installation. It also checks the discovered Compose version against the
running containers. The selected `compose_override_path` is bound to the
reviewed overlay; fresh observations must use that same path. The projected
Compose-generated replacement label is derived from the verified project,
service and container number. Existing replacement labels or conflicting
image-provided Compose labels refuse for separate review.
