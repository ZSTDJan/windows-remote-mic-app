# OrthoFocus Source Release Checklist

The source layout, dependency list, entry point, tests, attribution, and repeatable
export are prepared. The first public release is source-only and uses GPL-3.0-only.
For each public source update:

1. Preserve `LICENSE`, `COPYRIGHT.md`, `ATTRIBUTION.md`, and third-party notices.
2. Confirm the export contains no private paths, credentials, logs, or binaries.
3. Run the exported automated tests on Windows and complete manual checks in the
   intended host applications.
4. Keep executable packaging, signing, installers, tags, and GitHub Releases as
   separate explicitly reviewed release actions.
