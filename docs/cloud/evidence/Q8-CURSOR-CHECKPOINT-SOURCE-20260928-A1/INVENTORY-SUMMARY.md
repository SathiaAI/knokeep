# Display unit inventory (current policy)

## Accepted release rule
Owned units may be released only when **both** revision is `r3` **and** `signed_check` is `yes`.

## Rejected proposal (not policy)
An exception to allow signed `r2` units was rejected and is not current policy.

## Uncontested eligible owned units
Units: cedar, ash (`cedar`, `ash`).

## Held owned units (fail rule)
- **birch**: revision `r3`, signed_check `no`
- **maple**: revision `r2`, signed_check `yes`

## Disputed loan units (do not release)
- **elm**: meets r3+yes but ownership is `loan`; `Q_LOAN` unresolved — no `release-loan` authorization.
