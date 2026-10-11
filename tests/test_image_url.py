"""Catalog images: only a commit-pinned image file in the plugin's own repository."""

import copy

from jellysin_tooling.catalog import merge_manifest, validate_manifest
from jellysin_tooling.common import ValidationError, image_url, metadata
from tests.fixtures import FIRST, SECOND, Workspace

IMAGE = f"https://raw.githubusercontent.com/{FIRST['repository']}/{'b' * 40}/assets/logo.png"


class ImageUrlTests(Workspace):
    def test_descriptor_accepts_a_pinned_image_of_its_own_repository(self):
        self.assertEqual(IMAGE, metadata({**FIRST, "imageUrl": IMAGE})["imageUrl"])
        self.assertEqual(
            IMAGE.replace("logo.png", "Logo.WEBP"),
            image_url(IMAGE.replace("logo.png", "Logo.WEBP"), FIRST["repository"]),
        )

    def test_descriptor_rejects_mutable_foreign_or_unsafe_images(self):
        pinned = f"https://raw.githubusercontent.com/{FIRST['repository']}/{'b' * 40}"
        cases = (
            f"https://raw.githubusercontent.com/{FIRST['repository']}/main/assets/logo.png",
            f"https://raw.githubusercontent.com/{FIRST['repository']}/{'B' * 40}/logo.png",
            f"https://raw.githubusercontent.com/{SECOND['repository']}/{'b' * 40}/logo.png",
            IMAGE.replace("https://", "http://"),
            IMAGE.replace("raw.githubusercontent.com", "raw.githubusercontent.com.evil.test"),
            f"https://github.com/{FIRST['repository']}/releases/download/v1.0.0/logo.png",
            f"{pinned}/assets/logo.svg",
            f"{pinned}/assets/../logo.png",
            f"{pinned}/assets/logo.png?raw=true",
            f"{pinned}/assets/logo.png#fragment",
            f"{pinned}//logo.png",
            f"{pinned}/" + "a/" * 8 + "logo.png",
            f"{pinned}/",
            IMAGE + "\n",
            None,
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                metadata({**FIRST, "imageUrl": value})

    def test_first_import_copies_the_image_and_the_manifest_binds_it_to_the_source(self):
        release, _ = self.plugin({**FIRST, "imageUrl": IMAGE})
        plain, _ = self.plugin(SECOND)
        manifest = merge_manifest([], [release, plain])
        self.assertEqual(
            {FIRST["guid"]: IMAGE}, {entry["guid"]: entry["imageUrl"] for entry in manifest if "imageUrl" in entry}
        )
        self.assertEqual(manifest, merge_manifest(manifest, [release]))
        foreign = f"https://raw.githubusercontent.com/{SECOND['repository']}/{'b' * 40}/logo.png"
        for field, value in (("imageUrl", foreign), ("imageUrl", IMAGE.replace("b" * 40, "main")), ("homepage", IMAGE)):
            changed = copy.deepcopy(manifest)
            changed[0 if manifest[0]["guid"] == FIRST["guid"] else 1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                validate_manifest(changed)
        missing = copy.deepcopy(manifest)
        del missing[0]["owner"]
        with self.assertRaises(ValidationError):
            validate_manifest(missing)
