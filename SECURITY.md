# Security Policy

## Scope

This project is designed for **single-user, self-hosted, controlled-environment** operations.

Default assumptions:

- services listen on loopback by default
- deployment is typically fronted by Nginx / Caddy / another reverse proxy
- the operator is responsible for HTTPS, access control, and host hardening

## Built-in protections

- binds to `127.0.0.1` by default
- login requires username, password, and captcha
- sessions have an expiration window
- runtime state files are intended to stay out of Git

## Deployment recommendations

At minimum, you should:

- expose the service only to trusted users or trusted networks
- serve it behind HTTPS
- consider adding Basic Auth, WAF, or IP allowlists when appropriate
- keep runtime state, keys, tokens, and passwords out of the repository

## Reporting security issues

If you discover a security issue, please avoid publishing sensitive exploit details immediately.

A safer process is:

- contact the maintainer privately first
- share the minimal reproduction and impact
- avoid public disclosure of exploitable details until the issue is understood or fixed
