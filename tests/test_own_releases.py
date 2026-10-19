"""The CI step that holds this repository's own releases immutable, run against the stand-in gh."""

from pathlib import Path

from tests.test_release_pr_workflow import StepCase, forge

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def published(tag, immutable=True):
    return {"tag_name": tag, "draft": False, "immutable": immutable}


def state(releases, tags):
    return forge(releases=releases, tag_listing=[{"name": name} for name in tags])


class OwnReleaseTests(StepCase):
    step = "Check that this repository's releases are immutable and every version tag has one"
    workflow = CI

    def test_immutable_releases_pass_and_older_tags_are_named(self):
        lines = self.passed(state([published("v1.2.0"), published("v1.1.0")], ["latest", "v1.2.0", "v1.1.0", "v1.0.0"]))
        self.assertEqual(
            lines,
            [
                "v1.0.0: predates the first published release",
                "2 published releases, all immutable; every later version tag has one",
            ],
        )

    def test_a_mutable_release_fails(self):
        done = self.failed(state([published("v1.2.0", immutable=False)], ["v1.2.0"]), "1 release problem(s)")
        self.assertIn("v1.2.0: release is not immutable", done.stdout)

    def test_a_later_tag_without_a_published_release_fails(self):
        for releases in ([published("v1.1.0")], [published("v1.1.0"), {**published("v1.3.0"), "draft": True}]):
            with self.subTest(releases=releases):
                done = self.failed(state(releases, ["v1.3.0", "v1.1.0"]), "1 release problem(s)")
                self.assertIn("v1.3.0: version tag has no published release", done.stdout)

    def test_the_release_run_tag_passes_while_unpublished(self):
        # The read-only CI token never receives draft listings, so the hidden case is what the run sees.
        hidden = [published("v1.1.0")]
        shown = [published("v1.1.0"), {**published("v1.3.0"), "draft": True}]
        for releases in (hidden, shown):
            with self.subTest(releases=releases):
                lines = self.passed(state(releases, ["v1.3.0", "v1.1.0"]), GITHUB_REF="refs/tags/v1.3.0")
                self.assertEqual("v1.3.0: unpublished; this release run publishes it or fails", lines[0])

    def test_an_unpublished_tag_outside_its_own_release_run_fails(self):
        for ref in ("refs/tags/v1.1.0", "refs/heads/v1.3.0", "refs/tags/v1.3.0x", "refs/pull/7/merge"):
            with self.subTest(ref=ref):
                done = self.failed(
                    state([published("v1.1.0")], ["v1.3.0", "v1.1.0"]), "1 release problem(s)", GITHUB_REF=ref
                )
                self.assertIn("v1.3.0: version tag has no published release", done.stdout)

    def test_the_release_run_exempts_only_its_own_tag(self):
        tags = ["v1.3.0", "v1.2.0", "v1.1.0"]
        done = self.failed(state([published("v1.1.0")], tags), "1 release problem(s)", GITHUB_REF="refs/tags/v1.3.0")
        self.assertIn("v1.2.0: version tag has no published release", done.stdout)

    def test_the_superseded_tags_are_named_and_only_they(self):
        tags = ["v1.4.2", "v1.4.1", "v1.4.0", "v1.3.1"]
        done = self.failed(state([published("v1.3.1")], tags), "1 release problem(s)")
        for tag in ("v1.4.0", "v1.4.1"):
            self.assertIn(f"{tag}: tag kept with an unpublished draft; superseded by v1.4.2", done.stdout)
        self.assertIn("v1.4.2: version tag has no published release", done.stdout)

    def test_without_any_release_every_version_tag_fails(self):
        done = self.failed(state([], ["v1.0.0", "v1.0.1"]), "2 release problem(s)")
        self.assertIn("v1.0.0: version tag has no published release", done.stdout)

    def test_a_failed_or_full_listing_fails_without_echoing_it(self):
        self.failed(state([], []) | {"fail": "every"}, "Release listing request failed")
        self.failed(state([published(f"v1.0.{n}") for n in range(100)], []), "Unexpected release or tag listing")
