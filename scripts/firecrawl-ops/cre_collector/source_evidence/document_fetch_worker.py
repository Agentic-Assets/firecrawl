"""Internal one-shot document worker; a job is not acquisition authority."""

import argparse

from source_evidence import acquisition, automatic, document_fetch, events, private_export

JOB_FIELDS = {"schema_version", "url", "allowed_hosts", "max_bytes", "timeout_seconds", "max_redirects"}
JOB_SCHEMA = "cre-document-fetch-job/v1"


def validate_job(job):
    if (not isinstance(job, dict) or set(job) != JOB_FIELDS or job["schema_version"] != JOB_SCHEMA
            or not isinstance(job["allowed_hosts"], list) or not job["allowed_hosts"]
            or any(not isinstance(host, str) for host in job["allowed_hosts"])
            or len(job["allowed_hosts"]) != len(set(job["allowed_hosts"]))
            or type(job["max_bytes"]) is not int or not 1 <= job["max_bytes"] <= 100 * 1024 * 1024
            or type(job["timeout_seconds"]) is not int or not 1 <= job["timeout_seconds"] <= 60
            or type(job["max_redirects"]) is not int or not 0 <= job["max_redirects"] <= 5):
        raise ValueError("invalid document fetch job")
    for host in job["allowed_hosts"]:
        document_fetch._host(host)
    try:
        document_fetch._target(job["url"], job["allowed_hosts"])
    except document_fetch._Refusal:
        raise ValueError("invalid document fetch target") from None
    return job


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--job", required=True)
    parser.add_argument("--job-sha256", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    raw = automatic._read_private(args.job, 16384)
    if len(raw) > 16384 or events.sha256(raw) != args.job_sha256:
        raise ValueError("document fetch job digest mismatch")
    job, literals = acquisition.decode_json(raw, max_bytes=16384)
    if literals:
        raise ValueError("invalid document fetch job numbers")
    validate_job(job)
    result = document_fetch.fetch(job["url"], allowed_hosts=job["allowed_hosts"], max_bytes=job["max_bytes"],
                                  timeout_seconds=job["timeout_seconds"], max_redirects=job["max_redirects"])
    files = [("receipt.json", events.encoded({"job_sha256": args.job_sha256, "receipt": result["receipt"]}))]
    if result["receipt"]["outcome"] == "acquired":
        if not isinstance(result["body"], bytes):
            raise ValueError("missing acquired body")
        files.append(("document.bin", result["body"]))
    elif result["body"] is not None:
        raise ValueError("unexpected failure body")
    private_export.write(args.out_dir, files)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1) from None
