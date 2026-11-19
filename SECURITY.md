# Security policy

## Reporting a vulnerability
Please **do not open a public issue** for security problems.

Report privately through GitHub: on the repository page open **Security -> Report a vulnerability** (GitHub private vulnerability reporting / Security Advisories). Direct link once the repository is public: <https://github.com/mm-sp/PhoneBridge/security/advisories/new>.
Include the version, how to reproduce, and the impact. Please do not include real keys, phone numbers or message content.

You can expect an acknowledgement within a few days. This is a hobby project without a bounty program; fixes are made on a best-effort basis and credited in `CHANGELOG.md` if you wish.

## Supported versions
Only the latest release receives fixes.

## Scope and design
Architecture, tested properties and known limits (including that no independent human audit has been done, and that phone-to-PC control gives a paired phone a shell on the PC while it is enabled): `docs/SECURITY.md`. Signing identity and release process: `docs/SIGNING.md`.
