# CHANGELOG


## v2.5.4 (2026-07-16)

### Bug Fixes

- **engine**: Rollback restores the source's original URL
  ([`274a990`](https://github.com/bauer-group/IP-CoolifyMigration/commit/274a99082f3d89230ba038bf85a169ede4f383a7))

### Documentation

- Update README.MD [automated]
  ([`d71b692`](https://github.com/bauer-group/IP-CoolifyMigration/commit/d71b6921e421b72436054fa9acb16fcbfc63b011))


## v2.5.3 (2026-07-16)

### Bug Fixes

- **engine**: DNS gate reads the target and resolves server IPs
  ([`5aadf28`](https://github.com/bauer-group/IP-CoolifyMigration/commit/5aadf2899acd1f89dbeb459c33c8d0d28c3a5568))

### Documentation

- Update README.MD [automated]
  ([`056a960`](https://github.com/bauer-group/IP-CoolifyMigration/commit/056a96084cf279d8c6c2051a8e95b365849ab3db))


## v2.5.2 (2026-07-16)

### Bug Fixes

- **engine**: Freed source custom domains before create (409)
  ([`9ab11c6`](https://github.com/bauer-group/IP-CoolifyMigration/commit/9ab11c634989020fdabb9f5b8c3a852acc7fa500))

### Documentation

- Update README.MD [automated]
  ([`e726fd0`](https://github.com/bauer-group/IP-CoolifyMigration/commit/e726fd0ed80f49f2c05783216d0b983993349784))


## v2.5.1 (2026-07-16)

### Bug Fixes

- **engine**: Waited for target compose volumes before pairing
  ([`903291f`](https://github.com/bauer-group/IP-CoolifyMigration/commit/903291fa7fd170c481195f86e92d25d54e0aeb67))

### Documentation

- Update README.MD [automated]
  ([`3fe65a1`](https://github.com/bauer-group/IP-CoolifyMigration/commit/3fe65a159b8fd773d74f5662b2f3dcfeaaab3750))


## v2.5.0 (2026-07-16)

### Bug Fixes

- **planner**: Discovered volumes by project name, not uuid
  ([`99163cf`](https://github.com/bauer-group/IP-CoolifyMigration/commit/99163cffef467dd7f885d192554926949d448c70))

### Documentation

- Update README.MD [automated]
  ([`d599482`](https://github.com/bauer-group/IP-CoolifyMigration/commit/d5994820db34ea5911167583d832b240de7451b8))

### Features

- **doctor**: Warned when the Coolify version is untested
  ([`e5016a8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/e5016a88cf0eb84c8a42f29afa0e6cf694329d67))


## v2.4.2 (2026-07-16)

### Bug Fixes

- **api**: Remapped compose per-service domains
  ([`a21513e`](https://github.com/bauer-group/IP-CoolifyMigration/commit/a21513ebbe6ec55a197e17ed86aa7bf5b43aaeb3))

### Documentation

- Update README.MD [automated]
  ([`1a16985`](https://github.com/bauer-group/IP-CoolifyMigration/commit/1a16985c148da330e032f95252761e55f41d0965))


## v2.4.1 (2026-07-16)

### Bug Fixes

- **api**: Base64-encoded custom_labels on app create
  ([`ac757a8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/ac757a8aac92b7280816b9c8878c902fd2a22745))

### Documentation

- Update README.MD [automated]
  ([`8ebe128`](https://github.com/bauer-group/IP-CoolifyMigration/commit/8ebe12843d4f98bc362908f8f42456caf6ac181e))


## v2.4.0 (2026-07-16)

### Documentation

- Update README.MD [automated]
  ([`f649bb1`](https://github.com/bauer-group/IP-CoolifyMigration/commit/f649bb139cb57fac631eeecb0063c5c9f592110d))

### Features

- **dns**: Added --source/--target-wildcard overrides
  ([`24eda7c`](https://github.com/bauer-group/IP-CoolifyMigration/commit/24eda7c476264d6232afaea5be154b395d7a1d66))


## v2.3.0 (2026-07-16)

### Documentation

- Update README.MD [automated]
  ([`0d3cfd7`](https://github.com/bauer-group/IP-CoolifyMigration/commit/0d3cfd74c76319b02592b891b281ae8ffee10a4f))

### Features

- **dns**: Rewrote server-bound URLs onto target
  ([`3add035`](https://github.com/bauer-group/IP-CoolifyMigration/commit/3add035edf95eb47a6a3c7495d7797b6a069cfba))


## v2.2.3 (2026-07-16)

### Bug Fixes

- **engine**: Finish the no-volume migration path
  ([`0c1a4b8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/0c1a4b8684f6f872d01d72c1d0236e1be04ea63a))

### Documentation

- Update README.MD [automated]
  ([`764b31e`](https://github.com/bauer-group/IP-CoolifyMigration/commit/764b31e4b7d1dd73076634154bf97f5f5524131a))


## v2.2.2 (2026-07-16)

### Bug Fixes

- **engine**: Do not abort quiesce on a resource with no volumes
  ([`03e580e`](https://github.com/bauer-group/IP-CoolifyMigration/commit/03e580e0bee3eb0d6d831c57063b4e1bb742b265))

### Documentation

- Update README.MD [automated]
  ([`3a298d5`](https://github.com/bauer-group/IP-CoolifyMigration/commit/3a298d5fa2cc1e388e486f87455c27ae883000fe))


## v2.2.1 (2026-07-16)

### Bug Fixes

- **api**: Rebuild the git URL when recreating a public app
  ([`467c53d`](https://github.com/bauer-group/IP-CoolifyMigration/commit/467c53d4c13ca5e49693ac507fed051f868bc6e2))

### Documentation

- Update README.MD [automated]
  ([`a41bdca`](https://github.com/bauer-group/IP-CoolifyMigration/commit/a41bdca77c9a13912c9acf750a51c494802ac6bb))


## v2.2.0 (2026-07-16)

### Documentation

- Update README.MD [automated]
  ([`fcc64c1`](https://github.com/bauer-group/IP-CoolifyMigration/commit/fcc64c17e9bfc7ff93845fb9493514369fc86a94))

### Features

- Doctor server checks and rsync auto-install
  ([`1e226cd`](https://github.com/bauer-group/IP-CoolifyMigration/commit/1e226cda3afcb7cee8783aff195238f68c45505b))


## v2.1.5 (2026-07-16)

### Bug Fixes

- **ssh**: Record port-22 host keys under the bare hostname
  ([`e108e3a`](https://github.com/bauer-group/IP-CoolifyMigration/commit/e108e3a7de94295a9a0710214172204513915877))

### Documentation

- Update README.MD [automated]
  ([`80ee94d`](https://github.com/bauer-group/IP-CoolifyMigration/commit/80ee94d0a8f66bb41b7fe387aab4f1d3e6651908))


## v2.1.4 (2026-07-16)

### Bug Fixes

- **ssh**: Require a known_hosts path to trust a host key
  ([`9c9eb35`](https://github.com/bauer-group/IP-CoolifyMigration/commit/9c9eb35de891ca85ead2f774e8156dc3eb9eae59))

### Documentation

- Update README.MD [automated]
  ([`9be2595`](https://github.com/bauer-group/IP-CoolifyMigration/commit/9be259542136aa7696a82084352ced6738553c9c))


## v2.1.3 (2026-07-16)

### Bug Fixes

- **ssh**: Restore accept-any when trusting without a known_hosts file
  ([`2ac49c8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/2ac49c83a9579f06e55fdf1c85a2046c8a2e4a63))

### Documentation

- Update README.MD [automated]
  ([`0fbb124`](https://github.com/bauer-group/IP-CoolifyMigration/commit/0fbb124de8a10120cc0039daa584c9b4989d8150))


## v2.1.2 (2026-07-16)

### Bug Fixes

- **ssh**: Interactive host-key prompt and working TOFU
  ([`526ab0b`](https://github.com/bauer-group/IP-CoolifyMigration/commit/526ab0b28123404b9aef4675a35fefcd270982b8))

### Documentation

- Update README.MD [automated]
  ([`dc49e07`](https://github.com/bauer-group/IP-CoolifyMigration/commit/dc49e07203d13f347cb83490e911612400f237df))


## v2.1.1 (2026-07-16)

### Bug Fixes

- **cli**: Bare resource uuid and swallowed errors
  ([`71d5a1b`](https://github.com/bauer-group/IP-CoolifyMigration/commit/71d5a1b0270c596f68ff2bce62bc71579eb242f3))

### Documentation

- Update README.MD [automated]
  ([`79539fe`](https://github.com/bauer-group/IP-CoolifyMigration/commit/79539fea9690192e20ff4790fb5152534046e7b9))

### Testing

- **cli**: Covered uuid-based selection
  ([`aa28700`](https://github.com/bauer-group/IP-CoolifyMigration/commit/aa28700f8a29e084342cd1e9fe27663086677b72))


## v2.1.0 (2026-07-16)

### Documentation

- Update README.MD [automated]
  ([`cc85672`](https://github.com/bauer-group/IP-CoolifyMigration/commit/cc856725421eaa9d88dde5f4740a194c3a53939a))

### Features

- **cli**: Recursive listing, fixed prompt crash
  ([`a3bb1c4`](https://github.com/bauer-group/IP-CoolifyMigration/commit/a3bb1c44535fda27b569e77e67a31948a1418cd8))


## v2.0.0 (2026-07-16)

### Documentation

- Update README.MD [automated]
  ([`6a944c5`](https://github.com/bauer-group/IP-CoolifyMigration/commit/6a944c5fe605e666f2ee0afca838d353f1104279))

- **quickstart**: Added venv lifecycle guide
  ([`767dbe9`](https://github.com/bauer-group/IP-CoolifyMigration/commit/767dbe9a1678f40c45b5b6be51096588f1b10711))

### Features

- **cli**: Added scoped migration and discovery
  ([`50b55e8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/50b55e8fe90b39ed21d72c15b64665934865dd58))

### Testing

- **cli**: Covered picker, selection and confirm
  ([`eb3fac8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/eb3fac8555a3565ef12649c7fec606855b8e8e41))

### Breaking Changes

- **cli**: `plan`/`run <project>` now migrate the whole project (every environment). They previously
  defaulted to the `production` environment; use `<project>/production` or `--environment
  production` for the old behaviour.


## v1.0.2 (2026-07-16)

### Bug Fixes

- **gitattributes**: Add comment for generated text files
  ([`41ef22f`](https://github.com/bauer-group/IP-CoolifyMigration/commit/41ef22f7ae72f71df2a0a18fec32f3726c26390d))

### Build System

- Fixed changelog generation for PSR 10
  ([`c2a7689`](https://github.com/bauer-group/IP-CoolifyMigration/commit/c2a7689bcccf92b100c58e5b2058b99dc962b59d))

### Chores

- **ci**: Bump actions/cache from 5 to 6
  ([#2](https://github.com/bauer-group/IP-CoolifyMigration/pull/2),
  [`89290f2`](https://github.com/bauer-group/IP-CoolifyMigration/commit/89290f2f13da2f3bb4f3f45da6bc5bbf952b715a))

- **ci**: Bump actions/checkout from 6 to 7
  ([#1](https://github.com/bauer-group/IP-CoolifyMigration/pull/1),
  [`7ef96be`](https://github.com/bauer-group/IP-CoolifyMigration/commit/7ef96be9323b0b547c99cdbb22e11bf0dc1bf03b))

- **deps**: Update structlog requirement
  ([#3](https://github.com/bauer-group/IP-CoolifyMigration/pull/3),
  [`453d7c6`](https://github.com/bauer-group/IP-CoolifyMigration/commit/453d7c6dd4af698b3829dc4a41aebaa6b055df79))

- **deps**: Update structlog requirement from <26.0.0,>=24.4.0 to >=24.4.0,<27.0.0
  ([#3](https://github.com/bauer-group/IP-CoolifyMigration/pull/3),
  [`453d7c6`](https://github.com/bauer-group/IP-CoolifyMigration/commit/453d7c6dd4af698b3829dc4a41aebaa6b055df79))

### Continuous Integration

- Added a release-asset upload workflow
  ([`78f051e`](https://github.com/bauer-group/IP-CoolifyMigration/commit/78f051e1f51d01b53623f45d7a10160896372e0a))

- Disabled the org SECURITY.MD generator
  ([`bb97119`](https://github.com/bauer-group/IP-CoolifyMigration/commit/bb97119604bf7cefcaf39800a6a1add638c02b89))

- Fixed the security-scan workflow caller
  ([`1668417`](https://github.com/bauer-group/IP-CoolifyMigration/commit/16684177c59983304c75cd757cd8204c5eb6eee3))

- Removed the conflicting doc-check gate
  ([`94427e3`](https://github.com/bauer-group/IP-CoolifyMigration/commit/94427e37bf703572e7fa04710bb4e0e0c5b62cd9))

### Documentation

- Corrected the supported version to 1.0.x
  ([`b8f51a8`](https://github.com/bauer-group/IP-CoolifyMigration/commit/b8f51a80a8b699860604468825acc3f6e5a4a597))

- Update README.MD [automated]
  ([`95331f6`](https://github.com/bauer-group/IP-CoolifyMigration/commit/95331f636d40472725d4b457b9c2b85da321bc65))

- Update vulnerability reporting instructions in SECURITY.template.MD
  ([`bcdf8d1`](https://github.com/bauer-group/IP-CoolifyMigration/commit/bcdf8d114d4d7880a69b856fcbbbff7d39215314))

### Testing

- Synced the e2e runner to structlog <27
  ([#3](https://github.com/bauer-group/IP-CoolifyMigration/pull/3),
  [`453d7c6`](https://github.com/bauer-group/IP-CoolifyMigration/commit/453d7c6dd4af698b3829dc4a41aebaa6b055df79))


## v1.0.1 (2026-07-16)

### Bug Fixes

- **ci**: Fixed the integration rig on Linux CI
  ([`e38c9cd`](https://github.com/bauer-group/IP-CoolifyMigration/commit/e38c9cd100c79228922cf586abe37c239ccdf9ad))

### Documentation

- Update README.MD [automated]
  ([`ff78917`](https://github.com/bauer-group/IP-CoolifyMigration/commit/ff78917b42f6ec4de875eaf6316892cefccebb85))


## v1.0.0 (2026-07-16)

### Bug Fixes

- **core**: Aligned the tool with a real Coolify
  ([`3892288`](https://github.com/bauer-group/IP-CoolifyMigration/commit/389228843331e6377efcecceb109b2b96ba50206))

- **f2**: Defaulted an empty server user to root
  ([`682a3d4`](https://github.com/bauer-group/IP-CoolifyMigration/commit/682a3d482787fd79d8ed6b96acfcca691f8d1bbd))

- **f2**: Stopped cleanly, then probed patiently
  ([`087c1bc`](https://github.com/bauer-group/IP-CoolifyMigration/commit/087c1bcda4c748e584b6978a63572fc78393b042))

- **rollback**: Restarted source via /restart
  ([`d063a14`](https://github.com/bauer-group/IP-CoolifyMigration/commit/d063a1439dac98901a6e2c3cfa54857964cab968))

- **services**: Fixed compose service creation
  ([`48613ba`](https://github.com/bauer-group/IP-CoolifyMigration/commit/48613ba5da4414c1cc9d1783dc42c01007bb527f))

### Build System

- Moved development onto Python 3.14
  ([`b4f9538`](https://github.com/bauer-group/IP-CoolifyMigration/commit/b4f95385393169fe68cea8e4e932e0dfa63d4e26))

### Chores

- Ignored the e2e probe scratch file
  ([`35461e7`](https://github.com/bauer-group/IP-CoolifyMigration/commit/35461e748fff25c1e172759cce1ae1de713551f5))

- Ignored uv.lock, and said why
  ([`05cfdcd`](https://github.com/bauer-group/IP-CoolifyMigration/commit/05cfdcd7206edcd86c3d1ba9eeb0864f72444ce3))

### Documentation

- Documented the wired engine and the integration rig
  ([`ed0f100`](https://github.com/bauer-group/IP-CoolifyMigration/commit/ed0f10025630731d439d2aaea016c2422833fc75))

- Noted the instance API must be enabled
  ([`9db2676`](https://github.com/bauer-group/IP-CoolifyMigration/commit/9db2676cab5eff65b4919b5cd20759dcf73ed3e5))

- Recorded what the e2e rig disproved
  ([`f14869b`](https://github.com/bauer-group/IP-CoolifyMigration/commit/f14869be1ca639ee8953775a453ecaa04f66e6da))

- Recorded what the real F2 migration proved
  ([`8d773a5`](https://github.com/bauer-group/IP-CoolifyMigration/commit/8d773a557ace5f9ce6e5b18b0eef6b6548571909))

### Features

- **core**: Added Coolify migration toolkit foundation
  ([`e2a1ef5`](https://github.com/bauer-group/IP-CoolifyMigration/commit/e2a1ef5fe2d1020482b0f8d635d41d07b612f7ac))

- **engine**: Wired F1 and F2 end to end
  ([`dc8b580`](https://github.com/bauer-group/IP-CoolifyMigration/commit/dc8b580e6ffe56a6e7eca240c15a4b840068ef6a))

### Refactoring

- **drift**: Made drift advisory and added image-tag analysis
  ([`0488991`](https://github.com/bauer-group/IP-CoolifyMigration/commit/0488991581bf4c380fe97abf2c75729bda65e657))

### Testing

- **e2e**: Added a real Coolify test rig
  ([`caa7ab6`](https://github.com/bauer-group/IP-CoolifyMigration/commit/caa7ab6f9293d6bf18b5dee918e8f289dc81dc8c))

- **e2e**: Covered every engine and shape live
  ([`2890b76`](https://github.com/bauer-group/IP-CoolifyMigration/commit/2890b76a617d050723a99264818f1e2e1cd316a7))

- **engine**: Covered what the rig found, in units
  ([`3382e69`](https://github.com/bauer-group/IP-CoolifyMigration/commit/3382e69f898af857b608d7df872e60782c95cc02))


## v0.0.0 (2026-07-15)

- Initial Release
