# Workflows

## 1. Monthly close workflow

```text
1. Pull YouTube reports.
2. Normalize monthly channel revenue.
3. Pull AdSense payments.
4. Pull/enter tax and deduction data.
5. Enter bank received amount if available.
6. Calculate expected payment.
7. Compare expected payment vs AdSense payment.
8. Compare AdSense payment vs bank received.
9. Allocate unresolved deductions.
10. Generate channel/company/sector net revenue.
11. Review alerts.
12. Check `finance.lock_month`, call `MonthLock/lockMonth`, and create a `MONTH_LOCKED` audit event with the close reason.
13. Export reports.
```

## 2. Outside-CMS workflow

```text
1. Detect channel outside CMS.
2. Check if revenue is required.
3. Check if official revenue source exists.
4. If yes, import/normalize.
5. If no, mark as allocated or missing.
6. Show confidence warning.
7. Recommend CMS linking or manual official import.
```

Current foundation support:

- `GET /channels/outside-cms` lists outside-CMS channels visible to the caller's
  `analytics.view` scope.
- Missing official revenue is true only when revenue is required and the
  channel does not have `OFFICIAL_CMS_REVENUE` or `OFFICIAL_MANUAL_IMPORT`.
- The monitor recommends CMS linking or official manual import when official
  revenue is missing; manual-import channels remain visible so operations can
  keep the import current and continue pursuing CMS linkage.

## 3. Recalculation workflow

```text
1. User selects month.
2. User selects allocation method.
3. System validates finance permissions and source coverage.
4. System returns a dry-run recalculation preview with no financial writes.
5. User reviews blockers and source coverage.
6. Planned / not yet implemented: system recalculates deductions and net revenue after an explicit confirm action is added.
7. Planned / not yet implemented: system updates confidence levels and explanations after recalculation persistence is available.
8. Planned / not yet implemented: user accepts or reverts persisted results after the confirmation endpoint exists.
```

## 4. Export workflow

```text
1. User selects report type.
2. User selects scope: holding, sector, company, group, channel.
3. User selects month and currency.
4. System checks unresolved alerts.
5. System checks export and revenue visibility permissions for the requested scope.
6. Export job is created through `ExportService.enqueueExport`.
7. System creates an `EXPORT_CREATED` audit event.
8. File is generated.
9. User downloads report.
```

## 5. Manual override workflow

```text
1. Finance/admin opens month close.
2. System checks `finance.create_manual_override` or `finance.approve_manual_override` for the target channel.
3. User changes value.
4. System requires reason.
5. `ManualOverride/applyOverride` records old/new value and actor identity.
6. System creates `MANUAL_OVERRIDE_CREATED` or `MANUAL_OVERRIDE_APPROVED` audit event.
7. System recalculates affected numbers.
8. System flags report as override-used.
9. Override appears in export notes.
```
