"""Stand-in for the gh CLI: answers the release workflow's requests from a JSON state file and records them."""

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

PENDING, TAGGED = "autorelease: pending", "autorelease: tagged"
SECRET = "secret response body"


def read(state, path):
    # A test that lists the exact answers of its reads gets them as given; the paths below serve the rest.
    if path in state.get("responses", {}):
        return state["responses"][path]
    prefix = f"repos/{state['repo']}/"
    kind, _, name = path.removeprefix(prefix).rpartition("/")
    if kind == "git/matching-refs/tags":
        if state["listing"] is not None:
            return state["listing"]
        tags = state["tags"].items()
        return [{"ref": f"refs/tags/{tag}", "object": target} for tag, target in tags if tag.startswith(name)]
    if kind == "git/tags":
        return {"object": state["annotations"][name]}
    if kind == "pulls":
        return state["pulls"][name]
    if path == f"{prefix}releases?per_page=100":
        return state["releases"]
    if path == f"{prefix}tags?per_page=100":
        return state["tag_listing"]
    if path != f"{prefix}issues?state=closed&labels=autorelease%3A%20pending&per_page=20":
        raise SystemExit(f"stub gh: unexpected read {path}")
    pulls = state["pulls"].items()
    pending = [{"number": int(number), "pull_request": {}} for number, entry in pulls if PENDING in entry["labels"]]
    return pending + state["issues"]


def write(state, arguments):
    # A test that records issue writes gets each write recorded and a fixed issue number back.
    if "issue_writes" in state:
        state["issue_writes"].append(arguments)
        return {"number": 7}
    method, path = arguments[1], arguments[2]
    number = path.split("/")[4]
    labels = state["pulls"][number]["labels"]
    if method == "POST" and arguments[3:] == ["--raw-field", f"labels[]={TAGGED}"] and path.endswith("/labels"):
        labels.append(TAGGED)
        return [{"name": name} for name in labels]
    if method != "DELETE" or len(arguments) != 3:
        raise SystemExit(f"stub gh: unexpected write {arguments}")
    labels.remove(unquote(path.rsplit("/", 1)[1]))
    return None


def answered(state, arguments):
    if arguments[:2] == ["workflow", "run"]:
        return None
    if arguments[:2] == ["api", "--method"]:
        state["writes"].append(arguments[1:])
        return write(state, arguments[1:])
    if arguments[:1] != ["api"] or len(arguments) != 2:
        raise SystemExit(f"stub gh: unexpected command {arguments}")
    return read(state, arguments[1])


def main():
    path = Path(os.environ["GH_STUB_STATE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    arguments = sys.argv[1:]
    state["calls"].append(arguments)
    failing = state["fail"] in ("every", *arguments)
    answer = None if failing else answered(state, arguments)
    path.write_text(json.dumps(state), encoding="utf-8")
    if failing:
        # The real gh ends its error line with the HTTP status of a refused request, after the message.
        status = state.get("status", 502)
        print(json.dumps({"message": SECRET}))
        print(f"gh: {SECRET}" + (f" (HTTP {status})" if status else ""), file=sys.stderr)
        return 1
    if answer is not None:
        print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
