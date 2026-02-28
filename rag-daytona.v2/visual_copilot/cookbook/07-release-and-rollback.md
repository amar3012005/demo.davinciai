# 07 Release and Rollback

## Preconditions
- Backup manifest exists under `visual_copilot/backup/`.

## Release Steps
1. Verify compile/tests.
2. Restart services.
3. Run smoke mission scenarios.
4. Validate modular `vc.*` lifecycle events.

## Rollback Steps
1. Restore snapshot files from backup.
2. Re-point wrappers to previous owner if needed.
3. Restart containers.
4. Run smoke verification.

## Verification Checklist
- Plan endpoint healthy.
- Mission continuity and completion intact.
