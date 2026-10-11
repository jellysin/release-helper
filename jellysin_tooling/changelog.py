"""Cut one release's notes out of a release-please changelog as bounded, attested text."""

import re
from pathlib import Path

from .common import MAX_JSON, MAX_NOTES, ValidationError, release_notes, repository, require, semver

HEADING = re.compile(r"## \[?([0-9]+\.[0-9]+\.[0-9]+)\]?(?:[ (].*)?")


def section(document, version):
    """Return the lines between the version's level-two heading and the next one, or None."""
    lines = None
    for line in document.split("\n"):
        if line.startswith("## "):
            if lines is not None:
                break
            match = HEADING.fullmatch(line)
            if match and match.group(1) == version:
                lines = []
        elif lines is not None and (line.strip() or (lines and lines[-1])):
            # Trailing whitespace and repeated blank lines carry no meaning; drop them for a stable result.
            lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return lines or None


def bounded(lines, link):
    """Keep whole lines within the limit; a cut result ends with a link to the complete notes."""
    notes = "\n".join(lines)
    if len(notes) <= MAX_NOTES:
        return notes
    marker = f"[Full changelog]({link})"
    kept = []
    size = len(marker) + 1
    for line in lines:
        if size + len(line) + 1 > MAX_NOTES:
            break
        kept.append(line)
        size += len(line) + 1
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join([*kept, "", marker]).strip()


def notes(path, version, repo):
    """Read the version's notes from a changelog file; None when the file has no such section."""
    semver(version)
    repository(repo)
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_JSON + 1)
    require(len(data) <= MAX_JSON, "Changelog exceeds size limit")
    try:
        document = data.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("Invalid changelog encoding") from exc
    lines = section(document, version)
    if lines is None:
        return None
    return release_notes(bounded(lines, f"https://github.com/{repo}/releases/tag/v{version}"))
