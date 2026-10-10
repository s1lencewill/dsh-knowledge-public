# Release gates

Version: 0.1.0-alpha.4. Original-code license: Apache-2.0, author confirmed 2026-10-10.
Third-party materials retain their own terms. The npm private:true guard remains enabled.

- Public repository proposal: s1lencewill/dsh-knowledge-public; existing private history remains private.
- npm package identity: @s1lencewill/dsh-knowledge; account name supplied by author, login/publish rights not verified.
- Clean Node checkout install/build: locally demonstrated.
- Clean Python/runtime and clean Windows installation: NOT verified; existing dependencies cannot substitute.
- Python skipped tests: see docs/RELEASE-VALIDATION.md; all skipped security/engine checks are release-required.
- Marketplace frontend/backend/runtime updates: NOT verified; installed-artifact hashes do not prove market update synchronization.

Do not publish until the exact final artifact and all required release gates have passed.
