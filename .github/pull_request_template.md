## Summary

<!-- Explain why this change is needed and what outcome it provides. -->

## Files changed

<!-- List the important files or areas changed. -->

## Validation

<!-- List the exact commands run and their results. -->

## Storage policy notes

<!-- State whether DEPTSLM_DATA_DIR, persistent paths, or artifact handling changed. -->

- [ ] No `.env`, secrets, runtime artifacts, model files, or real department data are included.
- [ ] Tests use temporary storage rather than Google Drive.

## Security notes

<!-- Describe department_id, authorization, untrusted-document, or other security impact. -->

- [ ] Department-owned operations fail closed when scope is missing.
- [ ] Retrieved or uploaded content remains untrusted data.

## Remaining limitations

<!-- Identify deferred work, known gaps, and non-goals. -->

## Checklist

- [ ] The change is limited to one phase or coherent concern.
- [ ] Tests and documentation were updated where behavior changed.
- [ ] Planned capabilities are not presented as implemented.
- [ ] Staged changes were reviewed before commit.
