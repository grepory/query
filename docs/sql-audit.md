# SQL interaction audit — NOT-34

Audited on 2026-10-21 by the implementing agent and independently by Sol
(gpt-6.1-sol). No SQL injection vulnerabilities were found in the repository's
SQL interactions. The audit includes the pre-existing persistence implementation
and all supplied-evidence additions in NOT-34.

## Scope and findings

| Location | SQL interaction | Finding |
| --- | --- | --- |
| `src/query/store.py` | All reads, inserts, updates, row locks and PostgreSQL conflict handling | SQLAlchemy Core binds UUIDs, text, operation/document keys, metadata, JSON and limit-related fields as values. Table names, join conditions, ordering, and conflict targets are fixed expressions. No supplied value is concatenated into SQL syntax. |
| `src/query/db.py` | Table metadata, constraints and engine construction | Identifiers and SQL constraint expressions are fixed source constants. Database URLs configure the driver/connection, never query text. |
| `migrations/versions/0001_investigations.py` and `0002_evidence.py` | Alembic DDL and constraints | Fixed table names, types, foreign keys, indexes and checks. No supplied identifiers or payloads enter DDL. |
| `migrations/env.py` | Online/offline migration execution | Uses configured connections and fixed migrations. Offline literal rendering operates on migration definitions, not supplied research text. |
| `tests/integration/conftest.py` | CREATE/DROP SCHEMA and libpq search_path | Interpolation uses only `test_` followed by a newly generated UUID's hex digits. It cannot contain SQL delimiters. Neither environment variables nor application inputs supply the schema identifier. The test database is restricted to loopback `query_test`. |
| Tests, evals, configuration and repository scripts | Search for other SQL entry points | No additional dynamic SQL construction or execution entry points were found. Tests use the same Core expressions for inspection. |

The scan covered `execute`, `exec_driver_sql`, raw `text` expressions, SQL verbs,
query building, migration execution and connection/search-path configuration,
including hidden repository configuration. Supplied text, titles, URLs, identity
keys, selection reasons and JSON were traced to their execution sites.

## Verification

The PostgreSQL regression `test_sql_metacharacters_remain_literal_values` supplies
SQL statement delimiters and a DROP TABLE string in the operation key, document
key, title, origin URL, saved text, snapshot key and exclusion reason. They persist
as literal data, round-trip unchanged, and leave investigation tables intact.
Existing migration metadata comparison, upgrade/downgrade tests and the real
PostgreSQL persistence suite validate the actual SQL paths, rather than an SQLite
substitute. No new raw SQL or identifier quoting abstraction was needed.

This conclusion applies to the audited repository interactions. It does not make
claims about external services, future query code, or unsupported direct database
administration. Database roles and SQL console access retain their configured
permissions.
