"""Fixed entrypoint executed inside the keyless provisioning container."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

from preview_validation import validate_preview


def run_task(payload, query, emit=print):
    if payload.get("version") != 1 or payload.get("action") != "apply":
        raise ValueError("Unsupported provisioning task")
    deployment = payload["deployment"]
    common = (
        "deployment", "group", "--subscription", payload["subscription"],
        "--resource-group", deployment["resourceGroup"], "--name", deployment["name"],
        "--template-file", deployment["templateFile"], "--parameters", deployment["parametersFile"],
    )
    query(*common[:2], "validate", *common[2:], "--validation-level", "Provider")
    preview = query(
        *common[:2], "what-if", *common[2:], "--validation-level", "Provider",
        "--no-pretty-print", "--result-format", "FullResourcePayloads",
    )
    emit("MRKV_PREVIEW " + json.dumps(preview, separators=(",", ":")))
    validate_preview(preview)
    result = query(*common[:2], "create", *common[2:])
    if result.get("properties", {}).get("provisioningState") != "Succeeded" or not result.get("id"):
        raise RuntimeError("Privileged ARM deployment did not report Succeeded")
    return result["id"]


def main():
    raw = zlib.decompress(base64.b64decode(os.environ["MRKV_PAYLOAD"], validate=True))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != os.environ["MRKV_DIGEST"]:
        raise ValueError("Provisioning payload digest mismatch")
    payload = json.loads(raw)
    if payload["runId"] != os.environ["MRKV_RUN_ID"]:
        raise ValueError("Provisioning execution ID mismatch")
    deadline = time.monotonic() + payload["timeoutSeconds"]

    def query(*arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provisioning task deadline exceeded")
        result = subprocess.run(
            ["az", *arguments, "--output", "json", "--only-show-errors"],
            capture_output=True, text=True, timeout=remaining, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Azure command failed ({arguments[0]}): {result.stderr.strip()}")
        return json.loads(result.stdout)

    # The provisioning identity is selected explicitly; never use a caller token.
    query("login", "--identity", "--client-id", payload["clientId"])
    with tempfile.TemporaryDirectory() as directory:
        template = Path(directory) / "template.json"
        parameters = Path(directory) / "parameters.json"
        template.write_text(json.dumps(payload.pop("template")), encoding="utf-8")
        parameters.write_text(json.dumps(payload.pop("parameters")), encoding="utf-8")
        payload["deployment"]["templateFile"] = str(template)
        payload["deployment"]["parametersFile"] = str(parameters)
        deployment_id = run_task(payload, query)
    print("MRKV_RESULT " + json.dumps({
        "runId": payload["runId"], "digest": digest, "deploymentId": deployment_id, "status": "Succeeded",
    }), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError, TimeoutError, subprocess.TimeoutExpired, zlib.error) as error:
        print("MRKV_FAILURE " + json.dumps({
            "runId": os.environ.get("MRKV_RUN_ID"), "error": str(error),
        }), file=sys.stderr, flush=True)
        sys.exit(1)
