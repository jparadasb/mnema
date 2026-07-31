# Cloudflare

Optional `cloudflared` Compose profile is disabled by default. Supported hostname:

- `admin.example.com` for Mnema administration.

Configure an existing remotely managed tunnel with `sudo mnema configure cloudflare`.
Mnema stores the token as a secret file and does not automate Cloudflare account changes.
A separate local web service preserves emergency access.

The public web service requires `Cf-Access-Jwt-Assertion` on every HTTP request. It fetches
rotating account keys from the team-domain JWKS endpoint and validates RS256 signature,
issuer, application audience, expiry, issue time, and subject. Missing or invalid tokens
fail closed with HTTP 403.

SFTPGo web and raw SFTP remain local-only. Cloudflare routes must target
`http://web-public:8080`, never the local `web` service.
