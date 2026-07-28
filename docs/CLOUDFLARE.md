# Cloudflare

Optional `cloudflared` Compose profile is disabled by default. Intended hostnames:

- `admin.example.com` for Mnema administration.
- `files.example.com` for SFTPGo web access.

Provide tunnel token as secret file; Mnema does not automate Cloudflare account changes. Local emergency access remains.

Future public deployment must validate `Cf-Access-Jwt-Assertion`: signature against Cloudflare JWKS, issuer/team domain, audience, expiry, and clock skew. Header presence alone is not authentication.

