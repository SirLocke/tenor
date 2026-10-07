# Security Policy

## Supported Versions

We release security patches for the following versions:

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a Vulnerability

**Please do not open public issues for security vulnerabilities.**

If you discover a security vulnerability in tenor, please report it
responsibly by emailing **security@tenor.example.com** (replace with actual contact).

### What to Include

Please provide:

1. **Description** of the vulnerability
2. **Steps to reproduce** (minimal, complete)
3. **Impact assessment** (what could an attacker achieve?)
4. **Affected versions** (if known)
5. **Suggested fix** (if you have one)

### What to Expect

- **Acknowledgment** within 48 hours
- **Initial assessment** within 7 days
- **Regular updates** on progress
- **Coordinated disclosure** — we'll work with you on timing

### Disclosure Policy

- We aim to patch critical vulnerabilities within 30 days
- Public disclosure coordinated with reporter after fix is released
- Credit given to reporter (unless anonymity requested)

## Security Considerations for Users

### Secrets Management

- **Never commit** `config.local.yaml`, `.env`, `*.secret`, or Spotify credentials
- Use `spotify.env` in `~/.config/tenor/` with `chmod 600`
- Systemd units load `EnvironmentFile=-%h/.config/tenor/spotify.env`
- Override config via `TENOR_*` environment variables

### File Permissions

- Ingest/output directories: ensure only trusted users have write access
- Cache directories (`.cache/`) contain derived data, not secrets
- State file (`.tenor-state.json`) tracks processed files, not sensitive data

### Network Access

- **Nominatim (geocoding)**: Outbound HTTPS to `nominatim.openstreetmap.org`
- **Spotify API**: Outbound HTTPS to `api.spotify.com` (only if `spotify.enabled: true`)
- **Nextcloud scan**: Optional, runs configured `scan_cmd` (typically Docker exec)
- No inbound network services exposed by tenor itself

### Dependency Security

- Dependencies pinned in `pyproject.toml` / `requirements.txt`
- CI runs on every PR; Dependabot alerts monitored
- Report vulnerable dependencies via security email

## Scope

This policy covers the tenor codebase in this repository.
It does not cover:

- ffmpeg / system packages (report to their maintainers)
- Python itself (report to PSF)
- NAS / infrastructure configuration (your responsibility)

---

*Thank you for helping keep tenor and its users safe!*