# OrthoFocus Source Release Checklist

The source layout, dependency list, entry point, tests, attribution, and repeatable
export are prepared. The first public release is source-only and uses GPL-3.0-only.

In the Remote Mic source repository, export from `apps/windows/orthofocus` with
`tools/export-source.ps1 -Destination <dedicated directory>`. The destination
must be outside the repository, its ancestors, and the actual input source
directories. Directory links in the destination path or an existing export tree
are rejected, and DOS short names do not bypass these checks. `-Force` replaces
an existing export only after its paths have been checked again.

For each public source update:

1. Preserve `LICENSE`, `COPYRIGHT.md`, `ATTRIBUTION.md`, and third-party notices.
2. Confirm the export contains no private paths, credentials, logs, or binaries.
3. Run the exported automated tests on Windows and complete manual checks in the
   intended host applications.
4. Keep executable packaging, signing, installers, tags, and GitHub Releases as
   separate explicitly reviewed release actions.
