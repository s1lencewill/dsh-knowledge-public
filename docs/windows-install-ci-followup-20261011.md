# Windows install CI follow-up — 2026-10-11

Run 38075805717, source 2f9827e, failed at install-previous (exit 1). Both archives audited successfully. Cold build, zero-skip regressions, exact candidate SHA and update consistency passed. No install/upgrade acceptance is granted.

The install log was not uploaded, so the exact installer exception is unknown. resolveDshLaunch probes explicit DSH_BIN_DIR and two fixed Windows directories, whereas CI installs with npm global prefix. The workflow now supplies the actual npm global package directory explicitly, validates package.json exists, and uploads only the synthetic install-previous.log in addition to existing reports. This removes a host-location assumption without bypassing dependency/version checks. It is not proof of the prior failure's cause.

No product source changed in this follow-up. The new workflow commit changes package provenance, so generate a new reviewed candidate hash before dispatch. Old reports and packages remain. Repository and package stay private; daily service unchanged; no model calls.
