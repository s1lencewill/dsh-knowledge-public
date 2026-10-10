# Alpha release gates

Candidate: 0.1.0-alpha.5, Apache-2.0 for original code; third-party licenses retained. private:true stays enabled until all required gates pass. Repository stays private while Windows CI is pending.

Accounts verified: s1lencewill (GitHub and npm). Proposed public names: @s1lencewill/dsh-knowledge and s1lencewill/dsh-knowledge-public. No private project history is copied.

Build consistency: [contract](BUILD-CONSISTENCY.md). Local regressions and exact-package checks are recorded separately from a clean Windows run. Every skipped security test blocks release. The workflow has zero model calls and retains synthetic JSON reports, not runtime binaries or credentials.

A matching payload is not evidence of retrieval quality or of a live marketplace upgrade. Do not claim those capabilities from a unit test. Public release remains blocked until required Windows and host update evidence is complete.
