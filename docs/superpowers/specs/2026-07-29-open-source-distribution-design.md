# Open-Source Distribution Design

## Goal

Prepare this modified Xianyu management project for fee-based distribution as an open-source Windows installation package. The package may be sold on Xianyu as an installation package or installation service, while recipients retain the right to inspect, modify, and redistribute the project under the original AGPL-3.0 terms.

## Scope

- Keep the upstream `LICENSE` file and upstream attribution intact.
- Add a clear notice that this checkout is a modified derivative of `GuDong2003/xianyu-auto-reply-fix`.
- Publish the exact modified source in a public GitHub repository owned by the distributor, pinned by a release tag and commit hash.
- Make each Windows distribution archive point to that exact source version instead of bundling the source inside the archive.
- Keep runtime data out of the repository and archives: databases, cookies, browser sessions, logs, tokens, keys, email credentials, and seller-specific links.
- State that redistribution, modification, and independent installation are permitted under AGPL-3.0.
- Describe paid distribution as installation/support service, without claiming exclusivity or restricting downstream redistribution.

## Distribution contract

The installation archive will contain the executable source-adjacent files needed for the Windows setup flow, documentation, license notices, and a `SOURCE-CODE.md` pointer. `SOURCE-CODE.md` will contain the public repository URL, exact version tag, exact source commit, and a note that the corresponding source is available at no charge.

The public source repository will contain the complete corresponding source for the archive, including the Windows launchers, republish modules, tests, and documentation. It will not contain runtime data from the developer's machine.

The listing and package documentation will explicitly say that buyers receive no exclusive license and may redistribute or modify the project under AGPL-3.0. Paid value is limited to packaging, installation guidance, configuration, and support.

## Versioning and release flow

1. Run the existing sensitive-file and archive-safety checks.
2. Commit the distribution documentation and source-pointer template.
3. Create a clean public repository for this derivative.
4. Push the exact source commit and create a release tag.
5. Replace the source-pointer placeholders with the public repository URL, tag, and commit.
6. Re-run tests, build the Windows archive, and verify that the archive points to the exact published source.

## Non-goals

- No upload of the current seller's database, cookies, browser profile, logs, or private links.
- No closed-source licensing, DRM, activation server, or prohibition on redistribution.
- No claim that the project is an official Xianyu product or that it is guaranteed to remain compatible with Xianyu.

## Acceptance criteria

- The repository has an AGPL-3.0 license and a prominent derivative/modification notice.
- The repository README and package documentation explain the paid open-source distribution model.
- The package contains a valid source pointer with a fixed tag and commit.
- Sensitive-file/archive tests pass and no runtime state is published.
- The GitHub repository is public and its published commit matches the package source commit.
