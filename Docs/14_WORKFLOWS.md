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
12. Lock month.
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

## 3. Recalculation workflow

```text
1. User selects month.
2. User selects allocation method.
3. System recalculates deductions and net revenue.
4. System updates confidence levels.
5. System generates explanations.
6. User reviews differences.
7. User accepts or reverts.
```

## 4. Export workflow

```text
1. User selects report type.
2. User selects scope: holding, sector, company, group, channel.
3. User selects month and currency.
4. System checks unresolved alerts.
5. Export job is created.
6. File is generated.
7. Export is logged.
8. User downloads report.
```

## 5. Graph sync workflow

```text
1. Read updated SQL/warehouse rows.
2. Build graph nodes and relationships.
3. Upsert into Neo4j using sync writer.
4. Run validation checks.
5. Dashboard reads graph through read-only user/API.
```

## 6. Manual override workflow

```text
1. Finance/admin opens month close.
2. User changes value.
3. System requires reason.
4. System records old/new value.
5. System recalculates affected numbers.
6. System flags report as override-used.
7. Override appears in export notes.
```
