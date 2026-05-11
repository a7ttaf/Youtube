# Neo4j Graph Model

## Purpose
Create an awesome read-only graph that shows how UMS revenue flows from channel to company/sector/month/payment.

## Main node labels

```text
:Holding
:Sector
:Company
:ChannelGroup
:YouTubeChannel
:Month
:RevenueFact
:Payment
:Deduction
:Issue
:UserRole
```

## Main relationships

```text
(:Holding)-[:HAS_SECTOR]->(:Sector)
(:Sector)-[:HAS_COMPANY]->(:Company)
(:Company)-[:OWNS_CHANNEL]->(:YouTubeChannel)
(:ChannelGroup)-[:CONTAINS_CHANNEL]->(:YouTubeChannel)
(:YouTubeChannel)-[:HAS_REVENUE_FOR]->(:RevenueFact)
(:RevenueFact)-[:FOR_MONTH]->(:Month)
(:Payment)-[:COVERS_MONTH]->(:Month)
(:Payment)-[:RECONCILED_WITH]->(:RevenueFact)
(:Deduction)-[:APPLIED_TO]->(:RevenueFact)
(:Issue)-[:AFFECTS_CHANNEL]->(:YouTubeChannel)
(:Issue)-[:AFFECTS_MONTH]->(:Month)
```

## Node properties

### YouTubeChannel

```text
youtube_channel_id
name
cms_status
revenue_required
active
confidence
last_sync_at
```

### RevenueFact

```text
month
gross_revenue_usd
tax_usd
allocated_deductions_usd
net_revenue_usd
deduction_percentage
confidence
source_type
locked
```

### Payment

```text
month
adsense_payment_amount
currency
payment_date
payment_status
bank_received_amount
unresolved_gap
```

### Issue

```text
issue_type
severity
message
status
created_at
resolved_at
```

## Useful graph views

### 1. Company revenue flow

```cypher
MATCH path = (:Company {name: $company})-[:OWNS_CHANNEL]->(:YouTubeChannel)-[:HAS_REVENUE_FOR]->(:RevenueFact)-[:FOR_MONTH]->(:Month {month: $month})
RETURN path
```

### 2. Outside-CMS revenue problems

```cypher
MATCH path = (:Issue {issue_type: 'OUTSIDE_CMS_REVENUE_MISSING'})-[:AFFECTS_CHANNEL]->(:YouTubeChannel)
RETURN path
```

### 3. Payment gap explanation

```cypher
MATCH path = (:Payment {month: $month})-[:RECONCILED_WITH]->(:RevenueFact)<-[:APPLIED_TO]-(:Deduction)
RETURN path
```

### 4. Channels with low confidence

```cypher
MATCH (c:YouTubeChannel)-[:HAS_REVENUE_FOR]->(r:RevenueFact {month: $month})
WHERE r.confidence IN ['C_ALLOCATED', 'D_ESTIMATED', 'E_MISSING']
RETURN c, r
ORDER BY r.confidence
```

## Graph color rules

| Confidence | Graph color suggestion |
|---|---|
| A Official | Green |
| B Reconciled | Blue |
| C Allocated | Yellow |
| D Estimated | Orange |
| E Missing | Red |

## Graph size rule

- Channel node size = gross revenue.
- Issue node size = severity or unresolved amount.
- Payment node size = payment amount.

## Graph product views

1. **UMS hierarchy graph** — Holding → sectors → companies → channels.
2. **Revenue flow graph** — channel revenue → month → payment → deductions.
3. **Problem graph** — issues affecting channels/months.
4. **Outside-CMS graph** — only channels outside CMS and their revenue source status.
5. **Finance-close graph** — month, payment, deductions, unresolved gap.
