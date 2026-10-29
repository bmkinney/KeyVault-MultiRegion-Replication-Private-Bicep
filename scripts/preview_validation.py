"""Fail-closed structured what-if validation shared by delegated stages."""


def validate_preview(preview, *, allow_delete=False):
    if not isinstance(preview, dict) or preview.get("status") != "Succeeded" or preview.get("error") is not None:
        raise ValueError("What-if did not succeed")
    result = preview.get("properties", preview)
    if not isinstance(result, dict) or result.get("error") is not None:
        raise ValueError("What-if returned an invalid result")
    changes = result.get("changes")
    if not isinstance(changes, list):
        raise ValueError("What-if returned an unrecognized changes payload")
    for envelope in (preview, result):
        if envelope.get("diagnostics") or envelope.get("potentialChanges") or envelope.get("nextLink"):
            raise ValueError("What-if coverage is incomplete")
    # Ignore: an existing resource the stage template does not define; incremental mode never touches it.
    allowed = {"Create", "Modify", "NoChange", "Ignore"} | ({"Delete"} if allow_delete else set())
    for change in changes:
        if (
            not isinstance(change, dict) or change.get("changeType") not in allowed
            or not isinstance(change.get("resourceId"), str) or not change["resourceId"].strip()
            or change.get("unsupportedReason")
        ):
            raise ValueError("What-if contains unsupported, destructive, or unevaluated resources")
    return changes
