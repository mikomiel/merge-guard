# Changelog

## [0.3.0](https://github.com/mikomiel/merge-guard/compare/v0.2.1...v0.3.0) (2026-11-17)


### Features

* **release:** cover the merge_guard package in the release closure ([#67](https://github.com/mikomiel/merge-guard/issues/67)) ([2a92e75](https://github.com/mikomiel/merge-guard/commit/2a92e75a54a22f876cd1c6ca3dc44040c8b5ff44))

## [0.2.1](https://github.com/mikomiel/merge-guard/compare/v0.2.0...v0.2.1) (2026-11-16)


### Bug Fixes

* **engine:** close the issues the forge says a merge closes ([#63](https://github.com/mikomiel/merge-guard/issues/63)) ([699dbac](https://github.com/mikomiel/merge-guard/commit/699dbacc4964f8c603e0b32f5c3a4498c3079aed)), closes [#62](https://github.com/mikomiel/merge-guard/issues/62)

## [0.2.0](https://github.com/mikomiel/merge-guard/compare/v0.1.0...v0.2.0) (2026-11-16)


### ⚠ BREAKING CHANGES

* **forge:** a CLI run whose policy names no forge now exits 2 with policy_forge_missing; add repository.forge = "github" or pass --forge.

### Features

* **action:** publish whether a merge run landed a pull request ([#44](https://github.com/mikomiel/merge-guard/issues/44)) ([f9cf588](https://github.com/mikomiel/merge-guard/commit/f9cf58894bfcf417a39af5fd88b1ff349072d9b5)), closes [#43](https://github.com/mikomiel/merge-guard/issues/43)
* **classifier:** read the rules from a validated data file ([#11](https://github.com/mikomiel/merge-guard/issues/11)) ([#40](https://github.com/mikomiel/merge-guard/issues/40)) ([1b1a5d1](https://github.com/mikomiel/merge-guard/commit/1b1a5d1ee8d62dde02902033860d6d31c639ddc5))
* **forge:** put the forge behind one port with GitHub and Forgejo adapters ([#34](https://github.com/mikomiel/merge-guard/issues/34)) ([#47](https://github.com/mikomiel/merge-guard/issues/47)) ([71647ea](https://github.com/mikomiel/merge-guard/commit/71647ea18867baba7638ce195f339aa005f490d3))


### Bug Fixes

* **action:** pass inputs through the environment, not the shell source ([#42](https://github.com/mikomiel/merge-guard/issues/42)) ([3c863f1](https://github.com/mikomiel/merge-guard/commit/3c863f161db13cf91e8135279ce175a37f175444))
* name a required check that has no run ([#14](https://github.com/mikomiel/merge-guard/issues/14)) ([#39](https://github.com/mikomiel/merge-guard/issues/39)) ([d623857](https://github.com/mikomiel/merge-guard/commit/d623857b0fe6be5d312f97560c0eac98c0d0880d))

## [0.1.0](https://github.com/mikomiel/merge-guard/compare/v0.0.2...v0.1.0) (2026-11-14)


### Features

* package the engine and freeze the command line ([#30](https://github.com/mikomiel/merge-guard/issues/30)) ([ed13382](https://github.com/mikomiel/merge-guard/commit/ed1338274a280aa95ca515474a54597d8adc5943))
* **release:** adopt Release Please for the version, changelog and tag ([#32](https://github.com/mikomiel/merge-guard/issues/32)) ([c650ce3](https://github.com/mikomiel/merge-guard/commit/c650ce37c7b6e58709d0bfe0fe7bffce8b7bdc56))
* **release:** own the closure in a checked manifest ([#31](https://github.com/mikomiel/merge-guard/issues/31)) ([b0bb53b](https://github.com/mikomiel/merge-guard/commit/b0bb53bc624615e412db6c49c35a4f4951895929))
