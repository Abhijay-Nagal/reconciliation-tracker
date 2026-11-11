# Transaction Reconciliation & Exception Tracker

## 1. Your one-minute explanation

"I built a tool that compares internal and settlement transaction extracts using SQL. It groups references before joining so duplicate keys cannot create false matches. It identifies missing records, duplicate references, currency differences and amount differences. An analyst can investigate exceptions, save evidence and resolve or reopen them. Updates use a version check, and each status change and its event are committed together. On the synthetic demo, 2,981 references match and 25 require investigation."

This is reconciliation of two extracts, not a fraud model or a banking settlement engine. Exact key matching and explicit assumptions make it explainable within two weeks.

## 2. Why reconciliation exists

Two systems may describe the same business activity but disagree because of missing files, timing, duplicate exports, incorrect amounts or processing errors. Reconciliation asks whether records agree under a stated matching contract. When they do not, the output should identify what differs and preserve enough evidence to investigate.

An internal ledger here means one fictional source extract. Settlement means another fictional source extract. These names describe the scenario; no actual Amex ledger schema or settlement rules are implemented.

A match does not establish that both sources are correct. Two systems can contain the same wrong value. Reconciliation is one control alongside validation, ownership, source integrity and review.

The comparison contract is deliberately small: globally unique reference within each side, a nonnegative integer amount magnitude, and one of INR, GBP or SGD. It does not include transaction direction, fees, FX, partial settlements or multiple settlement dates. A real workflow would need those business rules before claiming a valid match.

## 3. Architecture and reading order

`two CSV files -> input contract -> hashed comparison -> raw entries -> SQL grouped join -> classification -> exception list -> analyst transition + event`

Read `reconcile.py` in this order: table definitions, `MATCH_SQL`, `parse_extract`, `classify`, `reconcile`, exception/history accessors, `change_status`, CSV export and CLI. Read `app.py` last.

`rec_runs` stores one comparison identity and its original summary. `rec_entries` stores input rows with primary key `(run_id,side,row_id)`. `reference` is not a unique database key because duplicate detection is a feature. `rec_exceptions` holds one outcome per problematic reference plus mutable investigation status/version. `rec_events` records transitions.

Immutable comparison results and mutable investigation state are separate concepts. A resolved exception remains a discrepancy in the original comparison. The original matching count does not increase when an analyst presses Resolve.

## 4. CSV parsing and monetary precision

The parser enforces exact ordered columns `reference,amount_minor,currency`, UTF-8 decoding, row shapes, size and row limits. References allow letters, digits, underscore and hyphen, up to 80 characters. This contract avoids ambiguous identifiers and deliberately excludes free-form descriptions.

Amounts accept 1-9 digits and become Python integers, then database integers. `1250` means 12.50 for the supported currencies. `12.50`, `1e3`, negative values and empty values are rejected, not rounded. A headers-only side is accepted: all references in the other file become missing-side exceptions. Two empty sides produce zero references, matches and exceptions.

Why integers? Binary floating point cannot represent many decimal fractions exactly. For a comparison like 12.50 against 12.50, integer minor units avoid representation ambiguity. This does not solve currency conversion, rounding policy or the varying minor-unit exponent of other currencies. Those are outside the stated contract.

Both extracts are parsed before database writes. A malformed settlement file cannot leave a committed ledger-only half-run. The 10 MB/100,000-row limits are portfolio guardrails, not a measured throughput claim.

## 5. The join problem you must understand

Suppose the ledger has R1 twice and settlement also has R1 twice. A direct join on reference produces four pairs. Summing those pairs can double totals or make duplicates look like legitimate multiple matches. The application avoids that by grouping each side first.

```sql
WITH l AS (
  SELECT reference, COUNT(*) AS n,
         MIN(amount_minor) AS amount, MIN(currency) AS currency
  FROM rec_entries
  WHERE run_id = :run AND side = 'ledger'
  GROUP BY reference
), s AS (
  SELECT reference, COUNT(*) AS n,
         MIN(amount_minor) AS amount, MIN(currency) AS currency
  FROM rec_entries
  WHERE run_id = :run AND side = 'settlement'
  GROUP BY reference
), refs AS (
  SELECT reference FROM l
  UNION
  SELECT reference FROM s
)
SELECT refs.reference,
       COALESCE(l.n, 0) AS ledger_count,
       COALESCE(s.n, 0) AS settlement_count,
       l.amount AS ledger_amount,
       s.amount AS settlement_amount,
       l.currency AS ledger_currency,
       s.currency AS settlement_currency
FROM refs
LEFT JOIN l ON refs.reference = l.reference
LEFT JOIN s ON refs.reference = s.reference
ORDER BY refs.reference;
```

A CTE gives an intermediate query a name. `UNION` removes duplicate keys; `UNION ALL` would retain them. Starting with every key and left-joining both grouped sides gives a portable full-outer-comparison pattern. `COALESCE` turns an absent count into zero. Absent amounts/currencies stay NULL rather than being silently treated as zero.

`MIN` is not a policy for choosing the correct duplicate. With count greater than one, classification returns duplicate before inspecting amount/currency. The UI's amount field is only representative in that case. To investigate duplicates, inspect all `rec_entries` rows for the run, side and reference.

## 6. Classification precedence

The `classify` function returns at most one category per reference:

1. Either count above one -> duplicate_reference.
2. Ledger count zero -> missing_ledger.
3. Settlement count zero -> missing_settlement.
4. Currencies disagree -> currency_mismatch.
5. Integer amounts disagree -> amount_mismatch.
6. Otherwise -> no exception.

Why check currencies first? Comparing 100 INR against 100 GBP as though the numbers have the same meaning would be wrong. A reference with both currency and amount differences receives currency_mismatch first. A duplicated key with a missing other side receives duplicate_reference first. This prioritization produces one actionable bucket, but secondary problems can remain behind the first classification.

The number of joined reference rows equals matched references plus exception references. Raw row counts can differ from reference counts because of duplicates. Always name the unit in a metric.

## 7. Know the deterministic fixture

The generator creates 3,000 ledger rows. Settlement starts with those rows, removes five, changes seven amounts and four currencies, adds three duplicate rows and six settlement-only rows. The edits use disjoint reference groups.

`3000 - 5 + 3 + 6 = 3004` settlement rows.

There are 3,006 unique references across both extracts: the original 3,000 plus six new keys. Exceptions: 5 + 7 + 4 + 3 + 6 = 25 reference outcomes. Matches: 3,006 - 25 = 2,981.

Raw control totals are grouped by side and currency and include duplicate rows. They can help establish extract populations but do not mean reconciled spend. There is no cross-currency total. A difference in raw totals is a signal, not a substitute for reference-level matching; offsetting errors can cancel out in totals.

The fixture's amount mismatches are +25 minor units. That is a deliberate demonstration, not evidence of an actual 25-unit business loss. No financial impact claim appears on the resume.

## 8. Idempotency and snapshots

The run ID hashes the ordered pair of source-file hashes and a matching-rule version. Swapping ledger and settlement changes the identity because the sides have different meanings. Identical bytes return the stored summary instead of rebuilding exceptions.

This matters after investigations exist: a rerun must not reset every status to OPEN. A changed file creates a new run with new exceptions; the application does not infer continuity of old investigations across different input snapshots.

Byte-identical idempotency does not recognize semantically identical files with reordered rows. The first run is atomic: run row, input rows, computed exceptions and summary are committed together. Concurrent identical first runs may race on the primary key; a queue or conflict/retry policy is a future extension.

## 9. Status workflow and concurrency

Allowed paths are OPEN -> INVESTIGATING -> RESOLVED, INVESTIGATING -> OPEN, and RESOLVED -> INVESTIGATING. There is no direct OPEN -> RESOLVED path. A reviewer and evidence note are required. A typed reviewer is a local-demo label, not authenticated identity.

Each exception starts with version 0. The browser submits the version it observed. The code reads the current record, validates the transition, then executes an update with both exception ID and expected version in its WHERE condition. Success increments the version.

```sql
UPDATE rec_exceptions
SET status = :new_status, version = :next_version
WHERE exception_id = :id AND version = :expected_version;
```

If another analyst saved first, the affected-row count is zero. The application raises an error and asks the user to refresh. This is optimistic concurrency: it detects a conflict when saving, rather than keeping a long-lived row lock while a human reads a page.

After the update, the event records old/new state, reviewer, note, time and new version in the same transaction. If event insertion fails, the state change rolls back. The event stream is append-only through the application, but a database administrator can still modify it. It is not cryptographically immutable evidence.

Resolving an exception records a judgment and note. It does not edit source rows or automatically validate a corrected file. A stronger future workflow would attach the corrected batch and require a new successful comparison before closure.

## 10. Exports and operational boundaries

The CLI writes summary JSON and exception CSV. The dashboard exports current exceptions and selected investigation history. User-written note fields that start with formula-trigger characters are prefixed when exporting to CSV so a spreadsheet is less likely to execute them as formulas. Stored text is not changed.

Bound SQL values protect data lookups from injection. This is separate from authentication, authorization, network security and data retention. Those are not implemented in this local tool. Do not describe the reviewer box as access control.

## 11. Five-minute demo

1. Explain the two input contracts and integer money representation.
2. Run the bundled comparison and identify rows versus references.
3. Show five exception categories and the 2,981/25 split.
4. Pick a duplicate reference and explain why it was not matched arbitrarily.
5. Pick a missing-settlement reference, enter a reviewer and evidence note, then move to INVESTIGATING.
6. Add a resolution note and move to RESOLVED. Explain why the original exception total stays 25.
7. Rerun identical files and show that the investigation history remains.
8. Download exceptions and show the per-currency control totals.

For a concurrency demonstration, explain the stale-version test rather than claiming two authenticated users exist.

## 12. Questions with answer guidance

**Why exact matching?** The contract declares a common unique reference. Exact matching is deterministic, explainable and testable. Fuzzy matching needs a business-approved ambiguity policy, especially for money.

**What if reference is only unique per day?** Change the key to include date, market or another agreed scope. Using a reference outside its declared uniqueness scope creates false duplicates or false matches.

**Why not compare sums only?** Offset errors can cancel; duplicate and missing rows can have the same total. Reference-level evidence tells the reviewer what differs.

**Why not use floating point with a tolerance?** For this fixed minor-unit contract, exact integers are simpler. Tolerances can hide differences and need business justification. Real FX/fee cases require explicit precision and rounding rules.

**What is a many-to-many join problem?** Duplicate keys on both sides multiply rows. Grouping first preserves one comparison row per reference and exposes counts.

**Why retain an exception after resolution?** The source comparison is historical evidence. Resolution is investigation state, not a rewritten history of what originally matched.

**Can two analysts overwrite each other?** A stale expected version fails the conditional update; event and status remain atomic. This is tested. There is no authenticated analyst identity in the demo.

**What does idempotent mean here?** Identical input bytes and matching version reuse a comparison without duplicating exceptions or erasing investigation state. It does not promise semantic identity across reserialized files.

**What if one extract is empty?** A valid header-only file is accepted and every key on the populated side becomes an exception. A malformed file is rejected rather than interpreted as empty.

**Why no auto-resolution?** Missing records can reflect timing, schema problems or actual errors. The correct action depends on business context and evidence. The demo records human investigation.

**How would you scale?** Add source indexes, bulk loading, a job model and partitioned snapshots, inspect query plans, and define retention. These are proposals; no production scale benchmark has been measured.

**What would you add next?** Composite matching keys, direction/fees, timing windows, authenticated reviewers, file lineage and corrected-run evidence. Add one extension only after defining its business semantics.

## 13. Exercises and readiness

Exercise A: make ledger contain two R1 rows and settlement three R1 rows. Predict one duplicate exception, not six matches. Write a direct join to observe the multiplication, then compare the grouped query.

Exercise B: change a reference's amount AND currency. Predict the chosen category and explain which secondary problem remains to investigate.

Exercise C: submit a valid status change with version 0, then another with version 0. Confirm the second fails and history has one event. Explain atomicity versus concurrency control.

Exercise D: remove all data rows on one side and run it. Check reference conservation: matched + exceptions = union of keys.

Exercise E: write SQL to list unresolved exceptions per category, then explain why that is a workflow report rather than a matching-quality metric.

You are ready when you can reproduce the fixture arithmetic, draw the grouped join, explain all state transitions, show why money is stored as integers, and state what this tool does not validate.

Reference documentation: https://www.postgresql.org/docs/current/queries-table-expressions.html ; https://docs.sqlalchemy.org/en/20/core/connections.html ; https://docs.streamlit.io/develop/api-reference/app-testing .
