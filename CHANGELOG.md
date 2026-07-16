# CHANGELOG


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
