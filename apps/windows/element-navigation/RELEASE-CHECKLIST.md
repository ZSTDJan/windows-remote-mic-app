# Standalone Release Checklist

The source layout, dependency list, entry point, tests, attribution, and repeatable
export are prepared. Before a public release:

1. Select and add a project license. The current source lives in a GPL-3.0-only
   repository, so GPL-3.0-only is the safe default unless a complete rights and
   provenance review confirms another license is permitted.
2. Review and bundle required third-party license texts and notices.
3. Create a dedicated Git repository only after its root and included files are
   confirmed; creating a GitHub repository is a separate optional step.
4. Run the exported automated tests on Windows and complete manual checks in the
   intended host applications.
5. Decide whether the first release is source-only or includes an unsigned Windows
   executable. Packaging, signing, uploading, tagging, and creating a Release are
   separate release actions.
