# Windows Developer Alpha readiness

Original code is Apache-2.0; third-party terms are retained. Sirchmunk is the primary retrieval engine, with no embedding-model execution branch. Runtime binaries and model keys are not included.

Publish only the exact archive whose source commit, version and SHA256 match the final successful clean Windows reports. Candidate CI 38105510507 passed Python 637/637 with no skips, Node 369/369, archive installation/read/upgrade/rollback 29/29, installed payload 14/14 and mismatch checks 3/3. That was a private candidate; it does not certify bytes changed for public release. The public-preparation archive requires renewed acceptance.

Windows Server CI does not certify desktop interactions, login triggers, real marketplace updates, ordinary non-admin accounts or retrieval quality. Retrieval coverage and historical DEEP failures remain known limitations. Account authentication does not bypass npm 2FA. Publication status and the final immutable checksums are reported with the GitHub pre-release assets.
