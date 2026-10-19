# Changelog

## [1.3.1](https://github.com/jellysin/release-helper/compare/v1.3.0...v1.3.1) (2026-10-19)


### Bug Fixes

* **catalog:** name a malformed GUID instead of a generic ValueError ([#69](https://github.com/jellysin/release-helper/issues/69)) ([965bbb0](https://github.com/jellysin/release-helper/commit/965bbb084c8382c9015738427fdb0a64a1fec130))

## [1.3.0](https://github.com/jellysin/release-helper/compare/v1.2.3...v1.3.0) (2026-10-19)


### Features

* **catalog:** check a local fixture release with catalog-check ([#67](https://github.com/jellysin/release-helper/issues/67)) ([da261ab](https://github.com/jellysin/release-helper/commit/da261ab21b71c61ea3be32d30246e1f641f78f63))
* **prepare:** skip a version whose tag another writer already made ([#60](https://github.com/jellysin/release-helper/issues/60)) ([d8a7fe3](https://github.com/jellysin/release-helper/commit/d8a7fe39c09fc2846c08d08d3ee76ddc26f55906))


### Bug Fixes

* **catalog:** skip a release without metadata, checksums or SBOM ([#63](https://github.com/jellysin/release-helper/issues/63)) ([feb6a0d](https://github.com/jellysin/release-helper/commit/feb6a0da24445af11b53149feca1ff6836b41d5f))

## [1.2.3](https://github.com/jellysin/release-helper/compare/v1.2.2...v1.2.3) (2026-10-18)


### Bug Fixes

* **publish:** accept the reusable build-signed workflow as signer ([#58](https://github.com/jellysin/release-helper/issues/58)) ([97fe7e4](https://github.com/jellysin/release-helper/commit/97fe7e48c0e231909a4e27b717f09848577bb152))

## [1.2.2](https://github.com/jellysin/release-helper/compare/v1.2.1...v1.2.2) (2026-10-13)


### Bug Fixes

* declare the Python floor CI tests (requires-python &gt;=3.14) ([#52](https://github.com/jellysin/release-helper/issues/52)) ([d4232b7](https://github.com/jellysin/release-helper/commit/d4232b7543e07f3f187746e1b6a65c985d79a54f))
* fail the check action on a catalog value other than true or false ([#41](https://github.com/jellysin/release-helper/issues/41)) ([f27529e](https://github.com/jellysin/release-helper/commit/f27529e4793aad8061bc3a1c6ce36030a5453815))
* name the tool jellysin-release-helper in requests, SBOMs and messages ([#36](https://github.com/jellysin/release-helper/issues/36)) ([07632eb](https://github.com/jellysin/release-helper/commit/07632ebd4ba3b868cf3d0faffcf19d0b647fcada))
* point the HISS-11 exception at a workflow that exists ([#43](https://github.com/jellysin/release-helper/issues/43)) ([6d37e95](https://github.com/jellysin/release-helper/commit/6d37e956d5c82c8e51f9c87cfaeac0facb15f152))
* run the release workflow's programs on the pinned interpreter ([#38](https://github.com/jellysin/release-helper/issues/38)) ([b418aa2](https://github.com/jellysin/release-helper/commit/b418aa2aa79d6449ca0a0839f1c9ae0dc4563cc7))

## [1.2.1](https://github.com/jellysin/release-helper/compare/v1.2.0...v1.2.1) (2026-10-11)


### Bug Fixes

* mark a merged release PR as tagged when its version tag names the merge commit ([#34](https://github.com/jellysin/release-helper/issues/34)) ([5d0d276](https://github.com/jellysin/release-helper/commit/5d0d2768e7b66ffa984fd067ea8c8c9ffb685ced))
* **prepare:** take the created draft from the answer of its own write ([5d0d276](https://github.com/jellysin/release-helper/commit/5d0d2768e7b66ffa984fd067ea8c8c9ffb685ced))

## [1.2.0](https://github.com/jellysin/release-helper/compare/v1.1.0...v1.2.0) (2026-10-11)


### Features

* add server-check, which asks a real Jellyfin server whether it offers every entry ([#32](https://github.com/jellysin/release-helper/issues/32)) ([0c3db8f](https://github.com/jellysin/release-helper/commit/0c3db8f8a043f18f02905d3907c29491cbb89ac7))

## [1.1.0](https://github.com/jellysin/release-helper/compare/v1.0.3...v1.1.0) (2026-10-11)


### Features

* add prepare, which creates the release tag and one unpublished draft ([#29](https://github.com/jellysin/release-helper/issues/29)) ([c8f92cf](https://github.com/jellysin/release-helper/commit/c8f92cf59c5cacc89c35322b7834c0f073af9067)), closes [#20](https://github.com/jellysin/release-helper/issues/20)
* hold server references at the ABI floor and carry text changelogs ([#28](https://github.com/jellysin/release-helper/issues/28)) ([f85b1ee](https://github.com/jellysin/release-helper/commit/f85b1eea95e803ee2e649a1b5ec4e0c824ecfb60)), closes [#22](https://github.com/jellysin/release-helper/issues/22)

## [1.0.3](https://github.com/jellysin/plugin-tooling/compare/v1.0.2...v1.0.3) (2026-09-08)


### Bug Fixes

* validate draft assets by authenticated API identity ([#11](https://github.com/jellysin/plugin-tooling/issues/11)) ([bfe7e8f](https://github.com/jellysin/plugin-tooling/commit/bfe7e8f7151b614f78fcd49ec4da351483794bd6))

## [1.0.2](https://github.com/jellysin/plugin-tooling/compare/v1.0.1...v1.0.2) (2026-09-08)


### Bug Fixes

* recover attested drafts by stable release identity ([#9](https://github.com/jellysin/plugin-tooling/issues/9)) ([d6e1272](https://github.com/jellysin/plugin-tooling/commit/d6e127221eeb58b03626a261ee02929f5dd9a474))

## [1.0.1](https://github.com/jellysin/plugin-tooling/compare/v1.0.0...v1.0.1) (2026-09-08)


### Bug Fixes

* enforce Python size limits for repository helpers ([#5](https://github.com/jellysin/plugin-tooling/issues/5)) ([9513cbe](https://github.com/jellysin/plugin-tooling/commit/9513cbe2417869c17efbaf1ebd1932f13555b58f))
* preserve Python runtime compatibility in Renovate ([#7](https://github.com/jellysin/plugin-tooling/issues/7)) ([93adc60](https://github.com/jellysin/plugin-tooling/commit/93adc60c170d117e2e8708a7a1d39fd38f46a02c))
* validate release SBOMs and recover interrupted draft uploads ([#8](https://github.com/jellysin/plugin-tooling/issues/8)) ([a364909](https://github.com/jellysin/plugin-tooling/commit/a364909014348d2e2f1554970fba44c2afa53626))

## 1.0.0 (2026-09-08)


### Features

* establish reproducible multi-plugin release tooling ([#1](https://github.com/jellysin/plugin-tooling/issues/1)) ([aa296ab](https://github.com/jellysin/plugin-tooling/commit/aa296ab50b45aa9022b919c0d28299817d23c204))

## Changelog

Release-please records reviewed changes here when a release PR is prepared.
