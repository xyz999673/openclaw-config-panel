# Contributing

Thanks for your interest in improving this project.

Before opening a PR, please try to keep the repository friendly for public self-hosted use.

## Ground rules

- Do not commit runtime state files
- Do not commit real keys, tokens, passwords, private domains, or private IPs
- Prefer separating deployment changes from feature changes
- If you modify config-apply logic, include a minimal validation or reproduction note

## Project style

This repository currently favors a practical self-hosted style:

- deployable
- readable
- maintainable
- minimal leakage of machine-specific private details

## Suggested PR scope

Good PRs for this repository usually fall into one of these buckets:

- deployment polish
- documentation clarity
- provider discovery improvements
- runtime/config safety improvements
- UI usability improvements

## Before submitting

Please double-check:

- no runtime data is included
- no machine-local artifacts are included
- no screenshots or docs leak private deployment details
- Python files still pass basic compile checks
