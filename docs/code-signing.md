# Code signing policy

Free code signing provided by [SignPath.io](https://signpath.io), certificate by
[SignPath Foundation](https://signpath.org).

pinkas has applied for it. Until the Foundation accepts the project, the Windows installer is not signed,
and Windows says so when you run it ([The Windows app](windows.md#installing)).

## What is signed

- `pinkas.exe`, the app, and `pinkas-setup-<version>.exe`, its installer.
- Only what the `release-windows` workflow builds on GitHub's own runners from a version tag of this
  repository. Nothing built on anyone's own machine is ever signed.
- Every signing request is approved by hand, release by release.

The installer is published only on this repository's
[Releases](https://github.com/Solarden/pinkas/releases) page, with its SHA-256 in the release notes.

## Team roles

| Role | Who |
|---|---|
| Committers and reviewers | [Solarden](https://github.com/Solarden) |
| Approvers | [Solarden](https://github.com/Solarden) |

A change from anyone else is reviewed by a committer before it is merged.

## Privacy

This program will not transfer any information to other networked systems unless specifically requested
by the user or the person installing or operating it. See [Privacy](privacy.md).
