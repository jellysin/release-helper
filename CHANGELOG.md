# Changelog

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
