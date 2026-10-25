"""Validation of release-please config and manifest against pinned schema and Git history.

Vendored schema provenance:
Source: https://github.com/googleapis/release-please/blob/v17.6.0/schemas/config.json
Licence: Apache-2.0 (Google LLC)
"""

import json
import re
from pathlib import Path

from .common import ValidationError, decode_json, digest, read_json, require
from .github import command

RELEASE_PLEASE_VERSION = "17.6.0"
SCHEMA_SHA256 = "82ae5d0a805cd3e4c437ce7a4d3eae1e3db51706ad414c624cc749ca02e7c1c5"
MAX_DEPTH = 32
MAX_WORKFLOW_FILES = 200
MAX_WORKFLOW_BYTES = 1024 * 1024
PREPARE_ACTION_PATTERN = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?:jellysin/release-helper/actions/prepare@|\./actions/prepare(?:[\"'\s#]|$))",
    re.MULTILINE,
)


class UnsupportedSchemaError(ValidationError):
    """A schema construct this validator cannot check; anyOf never treats it as a mismatch."""


ALLOWED_SCHEMA_KEYWORDS = frozenset(
    {
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "definitions",
        "description",
        "enum",
        "format",
        "items",
        "properties",
        "required",
        "title",
        "type",
    }
)


def child_path(parent, key):
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if re.fullmatch(r"[\$a-zA-Z_][a-zA-Z0-9_-]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key)}]"


def load_schema(path=None):
    if path is None:
        path = Path(__file__).resolve().parent / "schemas" / f"release-please-config-{RELEASE_PLEASE_VERSION}.json"
    raw = Path(path).read_bytes()
    actual = digest(raw, "sha256")
    require(actual == SCHEMA_SHA256, f"Schema digest mismatch: expected {SCHEMA_SHA256}, got {actual}")
    return decode_json(raw)


def check_schema_keywords(schema, path):
    if not isinstance(schema, dict):
        return
    for key in schema:
        if key not in ALLOWED_SCHEMA_KEYWORDS:
            raise UnsupportedSchemaError(f"Unsupported schema keyword {key!r} at {path}")


def check_type(instance, schema, path):
    if "type" not in schema:
        return
    expected = schema["type"]
    if expected == "object":
        require(isinstance(instance, dict), f"Expected object at {path}")
    elif expected == "array":
        require(isinstance(instance, list), f"Expected array at {path}")
    elif expected == "string":
        require(isinstance(instance, str), f"Expected string at {path}")
    elif expected == "boolean":
        require(isinstance(instance, bool), f"Expected boolean at {path}")
    elif expected == "number":
        require(isinstance(instance, (int, float)) and not isinstance(instance, bool), f"Expected number at {path}")
    elif expected == "integer":
        require(isinstance(instance, int) and not isinstance(instance, bool), f"Expected integer at {path}")
    elif expected == "null":
        require(instance is None, f"Expected null at {path}")
    else:
        raise ValidationError(f"Unsupported schema type {expected!r} at {path}")


def check_enum(instance, schema, path):
    if "enum" not in schema:
        return
    allowed = schema["enum"]
    require(instance in allowed, f"Value {json.dumps(instance)} at {path} not in enum {allowed}")


def check_required(instance, schema, path):
    if "required" not in schema or not isinstance(instance, dict):
        return
    for prop in schema["required"]:
        require(prop in instance, f"Missing required property {prop!r} at {path}")


def resolve_ref(schema, root_schema, path):
    target = schema["$ref"]
    prefix = "#/definitions/"
    require(
        isinstance(target, str) and target.startswith(prefix),
        f"Only local #/definitions/ references supported at {path}",
    )
    name = target.removeprefix(prefix)
    definitions = root_schema.get("definitions", {})
    require(name in definitions, f"Unresolved definition {target} at {path}")
    return definitions[name]


def check_properties(instance, schema, path, stack, depth):
    if "properties" not in schema or not isinstance(instance, dict):
        return
    for prop, prop_schema in schema["properties"].items():
        if prop in instance:
            stack.append((instance[prop], prop_schema, child_path(path, prop), depth + 1))


def check_additional_properties(instance, schema, path, stack, depth):
    if "additionalProperties" not in schema or not isinstance(instance, dict):
        return
    allowed = schema.get("properties", {})
    extra = [k for k in instance if k not in allowed]
    rule = schema["additionalProperties"]
    if rule is False:
        for k in extra:
            raise ValidationError(f"Unexpected property {k!r} at {child_path(path, k)}")
    elif rule is not True:
        for k in extra:
            stack.append((instance[k], rule, child_path(path, k), depth + 1))


def check_items(instance, schema, path, stack, depth):
    if "items" not in schema or not isinstance(instance, list):
        return
    item_schema = schema["items"]
    for i, item in enumerate(instance):
        stack.append((item, item_schema, child_path(path, i), depth + 1))


def check_all_of(instance, schema, path, stack, depth):
    if "allOf" not in schema:
        return
    for sub in schema["allOf"]:
        stack.append((instance, sub, path, depth + 1))


def validate_candidate_step(instance, schema, path, depth, stack):
    require(depth <= MAX_DEPTH, f"Maximum validation depth exceeded at {path}")
    if schema is True:
        return
    if schema is False:
        raise ValidationError(f"Disallowed at {path}")
    require(isinstance(schema, dict), f"Invalid schema at {path}")
    check_schema_keywords(schema, path)
    # Candidates are checked without composition; refuse it rather than skip it.
    composed = sorted({"$ref", "allOf", "anyOf"} & schema.keys())
    if composed:
        raise UnsupportedSchemaError(f"Unsupported {', '.join(composed)} inside anyOf at {path}")
    check_type(instance, schema, path)
    check_enum(instance, schema, path)
    check_required(instance, schema, path)
    check_properties(instance, schema, path, stack, depth)
    check_additional_properties(instance, schema, path, stack, depth)
    check_items(instance, schema, path, stack, depth)


def validate_candidate(instance, candidate, path, depth):
    local_stack = [(instance, candidate, path, depth)]
    while local_stack:
        inst, sch, p, d = local_stack.pop()
        validate_candidate_step(inst, sch, p, d, local_stack)


def check_any_of(instance, schema, path, depth):
    if "anyOf" not in schema:
        return
    for candidate in schema["anyOf"]:
        try:
            validate_candidate(instance, candidate, path, depth + 1)
            return
        except UnsupportedSchemaError:
            raise
        except ValidationError:
            continue
    raise ValidationError(f"Value at {path} did not match any of the allowed schemas in anyOf")


def validate_node(instance, schema, root_schema, path, depth, stack):
    require(depth <= MAX_DEPTH, f"Maximum validation depth exceeded at {path}")
    if schema is True:
        return
    if schema is False:
        raise ValidationError(f"Disallowed at {path}")
    require(isinstance(schema, dict), f"Invalid schema at {path}")
    check_schema_keywords(schema, path)
    if "$ref" in schema:
        resolved = resolve_ref(schema, root_schema, path)
        stack.append((instance, resolved, path, depth + 1))
        return
    check_type(instance, schema, path)
    check_enum(instance, schema, path)
    check_required(instance, schema, path)
    check_all_of(instance, schema, path, stack, depth)
    check_any_of(instance, schema, path, depth)
    check_properties(instance, schema, path, stack, depth)
    check_additional_properties(instance, schema, path, stack, depth)
    check_items(instance, schema, path, stack, depth)


def validate_schema(instance, schema):
    stack = [(instance, schema, "$", 0)]
    while stack:
        inst, sch, path, depth = stack.pop()
        validate_node(inst, sch, schema, path, depth, stack)


def validate_config(value, schema=None):
    if schema is None:
        schema = load_schema()
    validate_schema(value, schema)


def is_ancestor(ancestor, descendant, root):
    try:
        command(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=str(root))
        return True
    except ValidationError:
        return False


def creates_release_tags(root):
    workflows_dir = Path(root) / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return False
    candidates = sorted(p for p in workflows_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml"))
    require(len(candidates) <= MAX_WORKFLOW_FILES, f"More than {MAX_WORKFLOW_FILES} workflow files")
    for path in candidates:
        require(path.stat().st_size <= MAX_WORKFLOW_BYTES, f"Workflow file {path.name} exceeds 1 MiB")
        raw = path.read_bytes()
        require(len(raw) <= MAX_WORKFLOW_BYTES, f"Workflow file {path.name} exceeds 1 MiB")
        contents = raw.decode("utf-8", errors="replace")
        if PREPARE_ACTION_PATTERN.search(contents):
            return True
    return False


def stale_last_release_sha(root, config, manifest):
    if "last-release-sha" not in config:
        return
    require(
        isinstance(manifest, dict) and list(manifest.keys()) == ["."],
        "Only manifest package '.' is supported when last-release-sha is set",
    )
    version = manifest["."]
    pkg_config = config.get("packages", {}).get(".", {}) if isinstance(config.get("packages"), dict) else {}
    inc_comp = pkg_config.get("include-component-in-tag", config.get("include-component-in-tag", False))
    require(not inc_comp, "include-component-in-tag is unsupported when last-release-sha is set")
    inc_v = pkg_config.get("include-v-in-tag", config.get("include-v-in-tag", True))
    tag = f"v{version}" if inc_v else version

    if not creates_release_tags(root):
        return

    is_shallow = command(["git", "rev-parse", "--is-shallow-repository"], cwd=str(root))
    require(
        is_shallow != "true",
        "Shallow clone: the last-release-sha check needs full history and tags; check out with fetch-depth: 0",
    )

    try:
        tag_commit = command(["git", "rev-parse", "--verify", f"{tag}^{{commit}}"], cwd=str(root))
    except ValidationError as exc:
        raise ValidationError(
            f"Release tag {tag!r} is absent although a workflow runs release-helper actions/prepare; "
            f"push tag {tag!r} on the release {version} commit, "
            "or remove last-release-sha from release-please-config.json"
        ) from exc

    last_sha = config["last-release-sha"]
    try:
        last_commit = command(["git", "rev-parse", "--verify", f"{last_sha}^{{commit}}"], cwd=str(root))
    except ValidationError as exc:
        raise ValidationError(f"last-release-sha {last_sha!r} is not a commit in this repository") from exc

    if last_commit == tag_commit:
        return

    if is_ancestor(last_commit, tag_commit, root):
        raise ValidationError(f"last-release-sha {last_commit} is older than release {version} tag commit {tag_commit}")

    require(
        is_ancestor(tag_commit, last_commit, root),
        f"last-release-sha {last_commit} is not in tag history of {tag_commit}",
    )


def check(root):
    root = Path(root)
    config_path = root / "release-please-config.json"
    if not config_path.is_file():
        return
    manifest_path = root / ".release-please-manifest.json"
    require(manifest_path.is_file(), "Missing required .release-please-manifest.json")
    config = read_json(config_path)
    manifest = read_json(manifest_path)
    validate_config(config)
    stale_last_release_sha(root, config, manifest)
