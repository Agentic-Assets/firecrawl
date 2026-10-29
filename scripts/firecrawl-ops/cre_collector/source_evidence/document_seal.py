"""Prepare and read a private document seal; caller owns context authentication.

Readback verifies sealed metadata, not original bytes after their deletion. The
caller must rederive the context from policy, receipt and body, or authenticate
it against a previously sealed marker before relying on this helper.
"""

import json
import os
import stat
from urllib.parse import urljoin, urlsplit

from source_evidence import cli, document_acquisition, events, private_export

MAX_FILE = cli.MAX_PLAN_BYTES


def _context_shape(context, artifact):
    if (not isinstance(context, dict) or context.get("schema_version") not in
            (document_acquisition.SCHEMA, "cre-document-acquisition-context/v2")
            or not isinstance(context.get("receipt"), dict)):
        raise ValueError("invalid document context")
    receipt = context["receipt"]
    if set(receipt) != document_acquisition.RECEIPT_KEYS:
        raise ValueError("invalid document receipt shape")
    outcome = receipt.get("outcome")
    if outcome == "acquired":
        if (artifact is None or receipt.get("completion") != "complete" or receipt.get("failure_code") is not None
                or type(receipt.get("body_bytes")) is not int or receipt["body_bytes"] < 0
                or not isinstance(receipt.get("body_sha256"), str)):
            raise ValueError("invalid acquired context")
    elif outcome == "failed":
        if (artifact is not None or receipt.get("completion") != "incomplete"
                or receipt.get("failure_code") not in document_acquisition.FAILURES
                or receipt.get("body_bytes") is not None or receipt.get("body_sha256") is not None):
            raise ValueError("invalid failed context")
    else:
        raise ValueError("invalid document receipt outcome")


def prepare(context, artifact=None, *, landing=None, complement=None):
    """Return ordered canonical file bytes and their deterministic marker."""
    _context_shape(context, artifact)
    files = [("acquisition.json", events.encoded(context))]
    if landing is not None:
        from source_evidence import acquisition, automatic, document_candidates
        receipt = context["receipt"]
        extraction = artifact.get("extraction", {}) if isinstance(artifact, dict) else {}
        if (extraction.get("status") != "failed" or extraction.get("error_code") != "unsupported"
                or not isinstance(landing, dict) or landing.get("schema") != "cre-source-page-context/v1"
                or landing.get("sourceUrl") != receipt.get("final_url")
                or landing.get("bodySha256") != receipt.get("body_sha256")
                or landing.get("bodyBytes") != receipt.get("body_bytes")):
            raise ValueError("landing page disagrees with acquired unsupported HTML")
        automatic._refuse_credential_fields(landing)
        document_candidates._safe_metadata(landing, decoded_source_key=context["source_key"])
        for script in landing.get("scripts", []):
            if not isinstance(script, dict):
                raise ValueError("invalid landing script")
            if script.get("textComplete") is True:
                text = script.get("text")
                if not isinstance(text, str):
                    raise ValueError("invalid landing script text")
                decoded, _ = acquisition.decode_json(text.encode("utf-8"), max_bytes=256 * 1024)
                automatic._refuse_credential_fields(decoded)
                document_candidates._safe_metadata(decoded, decoded_source_key=context["source_key"])
            elif script.get("textComplete") is not False or script.get("text") is not None:
                raise ValueError("incomplete landing script must not retain unscanned text")
        for link in landing.get("links", []):
            if not isinstance(link, dict):
                raise ValueError("invalid landing link")
            reference = link.get("href")
            if isinstance(reference, str):
                resolved = urljoin(receipt["final_url"], reference)
                if urlsplit(resolved).scheme in ("http", "https"):
                    if not (any(c.isspace() for c in resolved) and "?" not in resolved and "#" not in resolved):
                        document_candidates._safe_url(resolved, allow_fragment=True)
                    document_candidates._safe_metadata(resolved, decoded_source_key=context["source_key"])
        files.append(("landing-page.json", events.encoded(landing)))
    binding = plan = None
    if artifact is not None:
        if not isinstance(artifact, dict):
            raise ValueError("invalid converter artifact")
        enriched = {**artifact, "acquisition_context": context,
                    "acquisition_context_origin": "foreground_coordinator_not_converter",
                    **({"pageContext": landing} if landing is not None else {})}
        binding, plan = document_acquisition.bind_extraction(context, enriched)
        files += [("converter-artifact.json", events.encoded(artifact)), ("artifact.json", events.encoded(enriched)),
                  ("binding.json", events.encoded(binding)), ("plan.json", events.encoded(plan))]
    complement_binding = complement_plan = None
    if complement is not None:
        from source_evidence import doc_complement_process
        executable = complement.get("parser", {}).get("config", {}).get("executable")
        doc_complement_process.validate(complement, artifact, context["receipt"], context["source_key"], executable)
        enriched_complement = {**complement, "acquisition_context": context,
                               "acquisition_context_origin": "foreground_coordinator_not_converter"}
        complement_binding, complement_plan = document_acquisition.bind_extraction(context, enriched_complement)
        relationship = {"schema_version": "cre-document-complement-relationship/v1",
                        "kind": "complement_of", "anydoc_artifact_sha256": events.sha256(events.encoded(artifact)),
                        "complement_artifact_sha256": events.sha256(events.encoded(complement)),
                        "anydoc_capture_id": binding["document_capture_id"],
                        "complement_capture_id": complement_binding["document_capture_id"], "coverage": "unknown"}
        files += [("complement-converter-artifact.json", events.encoded(complement)),
                  ("complement-artifact.json", events.encoded(enriched_complement)),
                  ("complement-binding.json", events.encoded(complement_binding)),
                  ("complement-plan.json", events.encoded(complement_plan)),
                  ("complement-relationship.json", events.encoded(relationship))]
    if any(len(payload) > MAX_FILE for _, payload in files):
        raise ValueError("document seal file exceeds bound")
    marker = {"files": {name: events.sha256(payload) for name, payload in files},
              "plan_sha256": plan["plan_sha256"] if plan else None,
              "capture_id": binding["document_capture_id"] if binding else None}
    if complement_plan is not None:
        marker["complement"] = {"plan_sha256": complement_plan["plan_sha256"],
                                "capture_id": complement_binding["document_capture_id"]}
    return files, marker


def write(outdir, context, artifact=None, *, landing=None, complement=None):
    files, marker = prepare(context, artifact, landing=landing, complement=complement)
    private_export.write(outdir, files)
    return marker


def check_primary(directory, context, marker, artifact):
    if not isinstance(marker, dict):
        raise ValueError("primary seal marker missing")
    result = read(directory, expected_context=context, expected_marker=marker)
    if events.encoded(result["artifact"]) != events.encoded(artifact) or "complement" in result:
        raise ValueError("primary seal disagrees with final artifact")
    return result


def read(outdir, *, expected_context, expected_marker=None):
    """Verify exact private file set and canonical bytes; do not claim body rehash."""
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "getuid")):
        raise RuntimeError("document seal readback requires POSIX")
    directory = os.open(outdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("document seal directory is not private")
        names = set(os.listdir(directory))
        has_artifact = expected_context.get("receipt", {}).get("outcome") == "acquired"
        required = ({"acquisition.json", "converter-artifact.json", "artifact.json", "binding.json", "plan.json"}
                    if has_artifact else {"acquisition.json"})
        has_landing = "landing-page.json" in names
        has_complement = "complement-converter-artifact.json" in names
        if has_complement:
            if not has_artifact or not isinstance(expected_marker, dict) or "complement" not in expected_marker:
                raise ValueError("complement requires an authenticated seal marker")
            required.update({"complement-converter-artifact.json", "complement-artifact.json",
                             "complement-binding.json", "complement-plan.json", "complement-relationship.json"})
        if has_landing:
            if not has_artifact or not isinstance(expected_marker, dict) or "landing-page.json" not in expected_marker.get("files", {}):
                raise ValueError("landing page requires an authenticated seal marker")
            required.add("landing-page.json")
        if names != required:
            raise ValueError("document seal file set mismatch")
        values = {}
        for name in sorted(required):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > MAX_FILE):
                    raise ValueError("invalid private seal file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_FILE + 1)
                after = os.fstat(fd)
                named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_mode, item.st_nlink,
                                         item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                if len(raw) > MAX_FILE or identity(before) != identity(after) or identity(after) != identity(named):
                    raise ValueError("document seal file changed during readback")
                values[name] = raw
            finally:
                os.close(fd)
        artifact = json.loads(values["converter-artifact.json"]) if has_artifact else None
        landing = json.loads(values["landing-page.json"]) if has_landing else None
        complement = json.loads(values["complement-converter-artifact.json"]) if has_complement else None
        files, marker = prepare(expected_context, artifact, landing=landing, complement=complement)
        if any(values[name] != payload for name, payload in files):
            raise ValueError("document seal bytes mismatch")
        if expected_marker is not None and events.encoded(expected_marker) != events.encoded(marker):
            raise ValueError("document seal marker mismatch")
        if set(os.listdir(directory)) != required:
            raise ValueError("document seal file set changed during readback")
        named_dir = os.stat(outdir, follow_symlinks=False)
        final_dir = os.fstat(directory)
        if (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != (final_dir.st_dev, final_dir.st_ino, final_dir.st_uid, final_dir.st_mode) or (
                named_dir.st_dev, named_dir.st_ino, named_dir.st_uid, named_dir.st_mode) != (
                final_dir.st_dev, final_dir.st_ino, final_dir.st_uid, final_dir.st_mode):
            raise ValueError("document seal directory changed during readback")
        result = {"artifact": artifact, "marker": marker, "original_bytes_reverified": False}
        if landing is not None:
            result["landing"] = landing
        if complement is not None:
            result["complement"] = complement
        return result
    finally:
        os.close(directory)
