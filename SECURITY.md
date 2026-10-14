# Security

Tokens, client credentials, and exported health records are sensitive. Do not
include them in issues, pull requests, screenshots, or public logs. Report only
the command, application/Python version, OS, and sanitized error/status code.

This is an independent open-source project, not an official Withings product.
It uses Withings' official Public Health Data API; users must create and
authorize their own developer application. It is not affiliated with,
endorsed by, or supported by Withings.

For vulnerabilities, use GitHub's **Security → Report a vulnerability** when
private vulnerability reporting is enabled on
[yaeloss/withings-agent-sync](https://github.com/yaeloss/withings-agent-sync).
Do not publish credentials or exploitation details in an ordinary issue.

The application uses owner-only POSIX file modes, atomic JSON replacement,
process locks, validated OAuth state, and a loopback-only callback listener.
These are not encryption, a sandbox, or a HIPAA/GDPR compliance certification.
The owner, privileged users, permissive extended ACLs, and backups may still
have access. Use a trusted Linux/macOS machine and private local storage.

Only the current release is maintained. Live Withings authorization is a manual
release check; tests and CI use synthetic data and never require real tokens.
