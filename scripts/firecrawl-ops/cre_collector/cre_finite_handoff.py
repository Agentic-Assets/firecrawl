"""One foreground reviewed Observe, Authorize and Apply operation."""

import json
from types import SimpleNamespace

from cre_code_handoff_evidence import canonical_path, json_bytes, require, sha
from cre_code_handoff_git import local_path, read_file

_QUALIFIED = object()


class FiniteProof:
    """Private in-memory qualified facts; every use rereads their inputs."""

    def __init__(self, raw, packet, evidence, replay, enable, *, _token=None):
        from cre_maintenance_validation_context import EvidenceSeal

        require(_token is _QUALIFIED, "finite context must follow full qualification")
        self.raw, self.packet, self.evidence = raw, packet, evidence
        self.seal, self.replay, self.enable = EvidenceSeal(packet, evidence), replay, enable

    def extend(self, raw, packet, evidence, allowed):
        from cre_maintenance_validation_context import EvidenceSeal

        def unchanged():
            require(raw == self.raw and packet is self.packet and evidence is self.evidence
                    and self.seal.packet == json.dumps(packet, sort_keys=True, separators=(',', ':')).encode()
                    and self.seal.inputs == json.dumps(evidence.inputs, sort_keys=True, separators=(',', ':')).encode()
                    and self.seal.original == evidence.original
                    and all(evidence.original_payloads.get(k) == v for k, v in self.seal.payloads.items())
                    and set(evidence.original_payloads) - set(self.seal.payloads) <= set(allowed),
                    "finite evidence changed outside admitted inputs")
        unchanged()
        evidence.recheck()
        unchanged()
        self.seal = EvidenceSeal(packet, evidence)

    def recheck(self, raw, packet, evidence):
        require(raw == self.raw and packet is self.packet and evidence is self.evidence,
                "finite qualified context differs")
        self.seal.recheck(packet, evidence)
        if self.replay is not None:
            self.replay.recheck()


def prepare_finite(args, raw, packet, evidence, git, modules):
    """Fully qualify expensive retained facts before taking a fresh observation."""
    from cre_maintenance_validation_context import LoadedMaintenance, RecordingGit

    loaded = getattr(evidence, "_loaded_finite", None)
    require(isinstance(loaded, LoadedMaintenance), "finite handoff requires a fully loaded release")
    loaded.unchanged(raw, packet, evidence)

    if packet["schema"] == "cre-runtime-maintenance-v1":
        from cre_maintenance_validation_context import qualify_stack_replay
        from cre_runtime_maintenance import (
            maintenance_readiness,
            recheck_continuation_stack,
            validate_operation_paths,
            validate_prepared_images,
        )

        validate_operation_paths(packet, local_path(args.output))
        if packet["expected_images"]:
            validate_prepared_images(packet, evidence, git)
        git.development(dict(packet, selected=packet["new"]))
        recheck_continuation_stack(packet, evidence, git, raw=raw)
        replay = qualify_stack_replay(packet, evidence) if "continuation" in packet else None
        maintenance_readiness(packet, modules, git)
        enable = None
    else:
        from cre_code_handoff import now, readiness
        from cre_code_restoration_evidence import prepare_enable_context

        replay = RecordingGit(packet["programs"]["native_git"])
        enable = prepare_enable_context(packet, evidence, now, git=replay)
        git.clean(packet["roots"]["development"], packet["development"], tracking=packet["development"]["sha"])
        readiness(packet, *modules, git, evidence=evidence)
    evidence.recheck()
    return FiniteProof(raw, packet, evidence, replay, enable, _token=_QUALIFIED)


def observe_finite(args, raw, packet, evidence, git, modules, _prepared):
    from cre_code_handoff import observe_restoration
    from cre_runtime_maintenance import observe_maintenance

    observe_args = SimpleNamespace(**vars(args))
    observe_args.output = args.observation_output
    if packet["schema"] == "cre-runtime-maintenance-v1":
        observe_maintenance(observe_args, raw, packet, evidence, git, modules)
    else:
        observe_restoration(observe_args, raw, packet, evidence, git, modules)


def authorize_finite(args, raw, packet, evidence, _prepared):
    from cre_code_handoff import authorize
    from cre_runtime_maintenance import authorize_maintenance

    if packet["schema"] == "cre-runtime-maintenance-v1":
        authorize_maintenance(args, raw, packet, evidence)
    else:
        authorize(args, raw, packet, evidence, finite=_prepared)


def apply_finite(args, raw, packet, evidence, git, modules, prepared):
    from cre_code_handoff import apply_restoration
    from cre_runtime_maintenance import apply_maintenance

    if packet["schema"] == "cre-runtime-maintenance-v1":
        apply_maintenance(args, raw, packet, evidence, git, modules, finite=prepared)
    else:
        apply_restoration(args, raw, packet, evidence, git, modules, finite=prepared)


def run_finite(args, raw, packet, evidence, git, modules):
    """Run exactly once; every stage retains its existing admission checks."""
    require(packet.get("schema") in {"cre-runtime-maintenance-v1", "cre-code-restoration-v1"},
            "finite handoff input schema differs")
    observation = local_path(args.observation_output)
    require(canonical_path(str(local_path(args.observation))) == canonical_path(str(observation / "observation.json")),
            "finite observation path differs")
    authorization_path = local_path(args.authorization)
    paths = [observation, authorization_path, local_path(str(authorization_path) + ".pending"),
             local_path(args.output)]
    normalized = [canonical_path(str(path)).casefold().rstrip("/") for path in paths]
    require(len(set(normalized)) == len(paths)
            and all(not left.startswith(right + "/") for left in normalized for right in normalized if left != right)
            and all(not path.exists() and all(not ancestor.is_symlink() for ancestor in (path, *path.parents))
                    for path in paths),
            "finite output already exists or aliases")
    prepared = prepare_finite(args, raw, packet, evidence, git, modules)
    observe_finite(args, raw, packet, evidence, git, modules, prepared)
    observation_raw = read_file(args.observation)
    observation = json_bytes(observation_raw)
    digest_field = "release_sha256" if packet["schema"] == "cre-runtime-maintenance-v1" else "restoration_sha256"
    require(observation.get(digest_field) == sha(raw), "finite observation release differs")
    from cre_code_handoff import now

    if packet["schema"] == "cre-runtime-maintenance-v1":
        from cre_runtime_maintenance import validate_maintenance_observation

        validate_maintenance_observation(packet, evidence, observation, now())
    else:
        from cre_code_restoration_evidence import validate_restoration_observation

        validate_restoration_observation(packet, evidence, observation, now())
    # Pre-apply observation validates held XML against existing pinned inputs;
    # it does not admit any new transitive evidence references.
    prepared.extend(raw, packet, evidence, set())
    require(read_file(args.observation) == observation_raw, "finite observation changed")
    authorize_finite(args, raw, packet, evidence, prepared)
    authorization_raw = read_file(args.authorization)
    authorization = json_bytes(authorization_raw)
    require(authorization.get("observation_sha256") == sha(observation_raw),
            "finite authorization observation differs")
    from cre_code_handoff import check_boundary_times

    if packet["schema"] == "cre-runtime-maintenance-v1":
        from cre_runtime_maintenance import validate_maintenance_authorization

        validate_maintenance_authorization(raw, packet, evidence, observation_raw, authorization, now(),
                                           output=args.output, review_reader=read_file)
    else:
        from cre_code_restoration_evidence import validate_restoration_authorization

        validate_restoration_authorization(raw, packet, evidence, observation_raw, authorization, now(),
                                            review_reader=read_file)
    check_boundary_times(observation_raw, authorization_raw)
    require(read_file(args.observation) == observation_raw and read_file(args.authorization) == authorization_raw,
            "finite admitted input changed")
    review = authorization["review"]["path"]
    review_raw = read_file(review)
    require(sha(review_raw) == authorization["review"]["sha256"], "finite review changed")
    reference = json_bytes(review_raw)["body"]
    body = reference["path"]
    body_raw = read_file(body)
    require(len(body_raw) == reference["bytes"] and sha(body_raw) == reference["sha256"],
            "finite review body changed")
    for path, value in ((review, review_raw), (body, body_raw)):
        require(path not in evidence.original_payloads or evidence.original_payloads[path] == value,
                "finite review aliases retained evidence")
        evidence.original_payloads[path] = value
    prepared.extend(raw, packet, evidence, {review, body})
    apply_finite(args, raw, packet, evidence, git, modules, prepared)
