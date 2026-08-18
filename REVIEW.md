# Repository Review

A full review of `Goodreads-ReadDate-Hardcover-Importer` at commit `264ac02`
("version 0.2"), covering correctness, security, reliability and project
hygiene. Every finding below was reproduced against the original code before
being fixed, and every fix is covered by a test.

**Result: 33 findings — 5 correctness bugs, 1 privacy bug, 9 reliability
issues, 5 efficiency issues, 7 UX/robustness gaps, and 6 project-level gaps.**
All of them are fixed in this branch except four that are recommendations for
the maintainer to decide on (listed in *Recommendations, not changed*).

Line references are to the code **as it was before this review**.

---

## Table of contents

- [1. Correctness bugs](#1-correctness-bugs)
- [2. Security and privacy](#2-security-and-privacy)
- [3. Reliability](#3-reliability)
- [4. Efficiency](#4-efficiency)
- [5. Robustness and UX](#5-robustness-and-ux)
- [6. Code hygiene](#6-code-hygiene)
- [7. Project-level gaps](#7-project-level-gaps)
- [8. Recommendations, not changed](#8-recommendations-not-changed)
- [9. Verification](#9-verification)

---

## 1. Correctness bugs

### C1 — Books without a "Date Read" always fail to sync (`sync_hardcover.py:251-255`)

The GraphQL mutation declares `$dateFinished: date!` — non-nullable — but
`process_tsv_file` passed `date_finished` straight through, and `format_date`
returns `None` for a blank Date Read. Every book with a start date and no
finish date sent `dateFinished: null` and was rejected by the API.

The intent to fall back was already visible in the success message
(`{date_finished or date_started}`, line 367), but the fallback was never
applied to the value actually sent.

Reproduced — the mutation variables built for a row with a blank Date Read:

```
F -> mutation variables: {'journeyId': 5, 'dateStarted': '2024-01-02', 'dateFinished': None}
```

**Fix.** Hardcover needs both ends of a reading journey, so when only one date
is known it is now used for both. Rows with neither date are skipped and
counted; rows whose finish date precedes their start date are skipped with a
warning rather than being pushed to the API.

### C2 — A hidden Goodreads column aborts the entire export (`parse_goodreads.py:77-105`)

`process_review_row` chained `.find()` calls:

```python
row.find("td", class_="field date_read").find("span", class_="date_read_value")
```

`get_text_or_default` guards against a missing *span*, but not a missing *td*.
Goodreads only renders the columns the user has made visible, so if `Date Read`
(or `Number of Pages`, or any other) is hidden, the `td` is absent and the call
raises `AttributeError`. That exception is not caught by
`download_and_process_goodreads_data` (which only handles `HTTPError`), so the
run dies with a traceback — after the output file has already been truncated
and partially written.

This is the same scenario the "Date Read is empty" warning added in commit
`9649d15` was meant to help with; the warning could never be reached when the
column was missing rather than blank.

Reproduced:

```
REPRO 1 (missing date_read td): AttributeError 'NoneType' object has no attribute 'find'
REPRO 2 (missing num_pages td): AttributeError 'NoneType' object has no attribute 'find'
```

**Fix.** A `cell_text(row, field, span_class=..., tag=...)` helper returns `""`
for any absent cell. A per-row `try/except` in the download loop means one
malformed row is skipped with a warning instead of ending the export, and rows
with neither an ID nor a title are dropped rather than written as blank lines.

### C3 — "Book not found" reported as `list index out of range` (`sync_hardcover.py:186-193`)

```python
if ("data" in data
    and "user_book_reads" in data["data"]["book_mappings"][0]["book"]["user_books"][0]):
```

Both `[0]` subscripts raise `IndexError` in entirely normal situations: no
Hardcover edition is mapped to that Goodreads ID, or the book is mapped but not
on the user's shelf. The broad `except Exception` caught it and logged a
misleading message plus a dump of the whole API response — for potentially
every book in a library.

Reproduced:

```
A -> Error finding book: list index out of range   (no book_mappings)
B -> Error finding book: list index out of range   (mapping exists, empty user_books)
```

**Fix.** The response is walked defensively, and the two cases now produce
distinct, actionable messages: `No Hardcover book is mapped to Goodreads ID …`
and `Book found on Hardcover but no reading journey exists for it`. As a bonus,
all mappings are considered instead of only the first, since one Goodreads ID
can map to several Hardcover editions.

### C4 — Multiple read journeys: code contradicts the README (`sync_hardcover.py:202-214`)

The README states:

> If no matching finished date is found, the first journey will be used

The code did the opposite — it logged `No book journey found` and returned
`None`, so the book was skipped entirely.

Reproduced (two journeys, neither matching the requested finish date):

```
C -> Warning: multiple journeys found
     No book journey found
     None
```

Additionally, a journey list of length 0 fell through both branches and
returned `None` with no message at all.

**Fix.** The documented fallback is now implemented: prefer the journey whose
`finished_at` matches, otherwise use the first and say so. The empty case is
logged explicitly.

### C5 — "not set" treated as a real date (`parse_goodreads.py:33-38`)

`convert_date` only understood `%b %d, %Y` and returned anything else verbatim.
Goodreads writes `not set` for unset dates and `Mar 2020` for month-only dates,
so both landed in the TSV unchanged. Two consequences:

1. The Date-Read-empty warning in `main` tests `row.get("Date Read", "").strip()`
   for truthiness — `"not set"` is truthy, so the warning never fired for the
   very users it was written for.
2. `sync_hardcover.format_date` then logged `Warning: Could not parse date: not set`
   once per affected book.

Reproduced:

```
convert_date 'Mar 2020' -> 'Mar 2020'
convert_date 'not set'  -> 'not set'
```

**Fix.** `convert_date` normalises placeholders (`not set`, `unknown`, `-`) to
`""` and accepts month-only and long-month formats, anchoring month-only dates
to the first of the month. `format_date` in the sync script accepts the same
set plus `%m/%d/%Y`, so hand-edited TSVs work too.

---

## 2. Security and privacy

### S1 — Non-Goodreads cookies forwarded to Goodreads (`parse_goodreads.py:60-65`)

```python
if not cookie.get('hostOnly', False) or (cookie.get('domain', '').startswith('.goodreads.com')
                                          or cookie.get('domain', '') == 'www.goodreads.com'):
    cookie_dict[cookie['name']] = cookie['value']
```

The first clause short-circuits the domain check. Any cookie with
`hostOnly: false` — the common case for cookies scoped to a domain and its
subdomains — is included **regardless of which site it belongs to**. The README
explicitly instructs users to "export ALL cookies from Goodreads", and the
popular Cookie Editor "export all" flow produces a file covering the whole
browser profile. Session cookies for unrelated sites were therefore attached to
requests sent to `goodreads.com`.

The `.startswith('.goodreads.com')` test is also wrong on its own terms: it
matches a domain like `.goodreads.com.evil.example`.

**Fix.** `load_cookies` now allowlists on the domain only — exact
`goodreads.com` or a true subdomain — after stripping the leading dot, and
reports how many cookies it kept. Malformed entries and non-list files are
ignored with a warning instead of raising. A test asserts that Facebook- and
bank-domain cookies are dropped.

### S2 — Full API responses written to the log (`sync_hardcover.py:148, 219, 224`)

Error paths logged `response.text` in full into `log.txt`, a file users are
asked to attach when reporting problems. **Low severity** — the Hardcover
responses seen here do not echo the API key — but the volume is unbounded and
the content is not reviewed before being shared.

**Fix.** Response bodies in error paths are truncated to 500 characters.

---

## 3. Reliability

### R1 — No timeout on any Hardcover request (`sync_hardcover.py:95-99`)

`requests.post` was called without `timeout`, so a stalled connection hangs the
script indefinitely with no output. `parse_goodreads.py` already used
`timeout=30`; the sync script did not.

**Fix.** A configurable 30-second timeout, asserted by a test.

### R2 — Only HTTP 504 was retried (`sync_hardcover.py:101-116`)

`429 Too Many Requests` — the status that actually matters for a script issuing
one request per book against a rate-limited API — was not retried, nor were
`500`, `502` or `503`. Network exceptions (`ConnectionError`, `Timeout`) were
caught and returned immediately, with no retry at all, despite being the most
transient failures of the set.

**Fix.** `RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}` plus
`requests.exceptions.RequestException`, all with exponential backoff, and
`Retry-After` honoured on 429.

### R3 — GraphQL-level errors crashed the response parsing

Neither `get_user_id` nor `find_book_by_goodreads_id` checked for an `errors`
key. A GraphQL error returns HTTP 200 with `"data": null`, so the code
subscripted `None`:

```
E -> Error finding book: 'NoneType' object is not subscriptable
```

**Fix.** `_make_request_with_retry` now decodes the JSON, checks `errors`, and
returns the payload — so every caller gets uniform handling. Non-JSON bodies
are handled too. A failed `get_user_id` now points at the most likely cause
(a missing or malformed API key).

### R4 — Fragile pagination detection (`parse_goodreads.py:153-155`)

```python
total_pages = int(pagination.find_all("a")[-2].text.strip())
```

Positional indexing into the pagination widget: `IndexError` if it holds fewer
than two anchors, `ValueError` if the second-to-last anchor is not numeric, and
a silently wrong count if Goodreads changes the trailing links.

**Fix.** `find_total_pages` takes the maximum of all numeric anchor texts and
returns `None` when it cannot tell, which the caller already handles.

### R5 — A failed run destroyed the previous export (`parse_goodreads.py:134`)

The output file was opened with `"w"` — truncating it — before the first
network request. Any failure (bad user ID, expired cookies, no network) left an
empty or header-only file where a good export used to be.

**Fix.** Page 1 is fetched first; the output file is not opened until it
succeeds. A test writes a sentinel file, forces a `ConnectionError`, and
asserts the file is untouched.

### R6 — Page 1 downloaded twice (`parse_goodreads.py:149, 161-163`)

The first page was fetched to read the pagination widget, then fetched again as
the first iteration of the loop. An extra request per run against a site known
for rate-limiting scrapers.

**Fix.** The first page's parsed soup is reused. A test asserts exactly one
request for `page=1`.

### R7 — Only `HTTPError` handled during download (`parse_goodreads.py:180`)

`ConnectionError`, `Timeout` and `TooManyRedirects` escaped as tracebacks.

**Fix.** `RequestException` is handled, with the HTTP-specific guidance kept for
`HTTPError`. A failure on a later page stops the run but keeps the pages already
written.

### R8 — Log file left unclosed (`sync_hardcover.py:400-421`)

`logger.close()` was only reached on the happy path. An early `return` on a
missing API key, or any exception, skipped it — leaving `log.txt` without its
footer and, since writes were unbuffered only by luck, potentially missing
trailing content.

**Fix.** `Logger` is a context manager, flushes after every line, and is
idempotent on close. A test raises inside the `with` block and asserts the
footer is still written.

### R9 — Both scripts always exited 0

`main()` returned `None` on every path, so a failed export or a failed sync was
indistinguishable from success to any calling script.

**Fix.** `sys.exit(main())` with `0` success, `1` failure, `2` bad arguments.

---

## 4. Efficiency

### E1 — Every book rewritten on every run (`sync_hardcover.py:355-368`)

`dates_changed` was computed and then used **only to decide whether to increment
a counter** — the mutation itself ran unconditionally. Re-running the script
over a synced library issued a write for every book, for no change.

**Fix.** When the journey's dates already match, the mutation is skipped and the
book is counted as "already correct".

### E2 — Unused API call per book (`sync_hardcover.py:231-234`)

`update_reading_date` called `get_user_id()` and never used the result. Cached
after the first call, so cheap — but dead code that reads as a dependency.

**Fix.** Removed.

### E3 — Repeated DOM lookups (`parse_goodreads.py:99-104`)

`row.find("td", class_="field num_pages")` and `.find("nobr")` were each
evaluated twice per row for the conditional and the value.

**Fix.** Single lookup via `cell_text`.

### E4 — TSV read twice to produce a count (`sync_hardcover.py:309-311`)

The file was fully consumed to count rows, rewound, and re-read. The count was
then reported as "Total books evaluated" even though it included rows that were
skipped.

**Fix.** Counters accumulate during the single pass, and the summary
distinguishes processed / updated / already-correct / skipped / failed.

### E5 — Sleep after the final item

Both scripts slept a second after the last page and the last book. Now the delay
only falls *between* items.

---

## 5. Robustness and UX

### U1 — The `Bearer` prefix trap

The client passed the key straight into the `Authorization` header with the
comment *"The API key already includes 'Bearer'"*, while the README told users
to save the key as `"api_key": "your_api_key_here"`. Following the README gave
an unauthenticated key and an unhelpful `Could not fetch user ID` message.

**Fix.** `normalize_api_key` adds the prefix when absent; the README shows the
prefixed form and notes both work.

### U2 — `--limit=abc` produced a traceback (`sync_hardcover.py:412`)

**Fix.** Both `--limit` and `--delay` are validated with a clear message and
exit code 2, including negative values.

### U3 — Books with a read date but no start date were dropped (`sync_hardcover.py:329`)

`if not goodreads_id or not date_started_tsv: continue` skipped any book whose
Date Started was blank — a common state on Goodreads, and precisely the data
this tool exists to import.

**Fix.** Only the Goodreads ID is mandatory; a missing start date falls back to
the finish date (see C1).

### U4 — No validation of the TSV's columns

A file with different headers raised `KeyError` on every row, caught by the
per-row handler and reported 300 times.

**Fix.** Required columns are checked once, up front, with a pointer to
regenerate the file.

### U5 — No dry run on a tool that mutates a remote account

The only safety valve was `--limit`, which still writes.

**Fix.** `--dry-run` reports every intended change and issues no mutations. A
test asserts `requests.post` is never called.

### U6 — `cookies.json` path hard-coded

**Fix.** `--cookies=<file>`, defaulting to `cookies.json`.

### U7 — Summary conflated outcomes

"Total books evaluated / Books with dates updated" hid skips and failures
entirely.

**Fix.** Five distinct counters, plus a dry-run notice.

---

## 6. Code hygiene

| # | Issue | Fix |
| --- | --- | --- |
| H1 | Unused imports `sys` and `contextlib.contextmanager`; unused `is_mutation` parameter (`sync_hardcover.py:24, 26, 86`) | Removed (`sys` re-added for `sys.exit`) |
| H2 | `try: … except RequestException: raise` — a no-op that only obscures the flow (`parse_goodreads.py:67-72`) | Removed |
| H3 | The seven TSV column names duplicated between the writer and the reader | Single `FIELDNAMES` constant |
| H4 | The same error logged three ways (`Error type`, `Error details`, `str(e)`) at `sync_hardcover.py:374-378` and `383-386` | One line with the exception type and message |
| H5 | Request headers rebuilt for every page (`parse_goodreads.py:43-55`) | Set once on a reused `Session` |

---

## 7. Project-level gaps

### P1 — No tests

Nothing in the repository could be verified without a live Goodreads session and
a real Hardcover account, which is why the bugs above went unnoticed.

**Fix.** 81 tests in `tests/`, all using mocked HTTP — nothing contacts Goodreads
or Hardcover. They cover date parsing, row extraction with missing columns,
cookie filtering, pagination, retry/backoff and error handling, journey
selection, and the TSV processing loop.

### P2 — No CI

**Fix.** `.github/workflows/tests.yml` runs the suite on Python 3.9–3.12 for
pushes and pull requests.

### P3 — `requests==2.31.0` has known CVEs

- **CVE-2024-35195** — a `Session` that once made a request with `verify=False`
  silently skips certificate verification for all later requests to that host.
  Fixed in 2.32.0.
- **CVE-2024-47081** — a malicious URL can leak `.netrc` credentials to a
  third-party host. Fixed in 2.32.4.

The `beautifulsoup4==4.12.2` pin is also old enough to cause install friction on
recent Python versions.

**Fix.** Minimum-version constraints: `requests>=2.32.4`, `beautifulsoup4>=4.12.3`.

### P4 — `.gitignore` gaps

No entry for `.pytest_cache`, `.venv`, or the generated `books.tsv` — which
contains the user's full reading history and is easy to commit by accident.

**Fix.** All added.

### P5 — README claimed Python 3.6+

3.6 has been end-of-life since 2021 and was never tested.

**Fix.** 3.9+, matching the CI matrix.

### P6 — README described behaviour the code did not have

Beyond C4 (multiple journeys), the README's API-key example produced a key that
did not authenticate (U1).

**Fix.** README updated throughout: new options documented in tables, exit codes
described, the single-date and multiple-journey behaviours explained, cookie
filtering noted, and a Development section added.

---

## 8. Recommendations, not changed

These are the maintainer's call, so this branch leaves them alone.

1. **Release zips are committed to the repository.** `releases/GR_DatesRead_0.1.zip`
   and `0.2.zip` are binary blobs in git, and both contain macOS junk —
   `__MACOSX/` forks in both, plus a `.DS_Store` in 0.2. GitHub Releases is the
   better home for these; removing them from history is a rewrite and needs your
   decision.
2. **`docopt` is unmaintained** — its last release was 0.6.2 in 2014. The
   maintained drop-in fork `docopt-ng` needs no code changes beyond the
   requirement line. Left as-is to avoid changing what existing users install.
3. **Packaging.** A `pyproject.toml` with console-script entry points would let
   users `pip install` the tool and drop the `python script.py` invocations.
4. **Resumability.** A long sync that dies at book 400 of 500 starts over from
   the beginning. Recording processed Goodreads IDs would make re-runs cheap —
   though E1 (skipping unchanged books) already removes most of the cost.

---

## 9. Verification

```
$ pytest -q
81 passed
```

Beyond the unit tests, both entry points were exercised end to end against
mocked HTTP:

- `parse_goodreads.py` over a two-page library containing a hidden `Date Read`
  column, a `not set` value and a structurally empty row: it completes, issues
  exactly two requests (was three), writes only real rows, normalises `not set`
  to empty, and — now that the placeholder is normalised — correctly emits the
  "Date Read column is empty" guidance.
- `sync_hardcover.py --dry-run` against a missing TSV, a malformed `--limit`,
  and a valid file: correct messages and exit codes throughout, with no
  mutations issued.

### Files changed

| File | Change |
| --- | --- |
| `parse_goodreads.py` | Rewritten: C2, C5, S1, R4–R7, R9, E3, E5, H2, H3, H5, U6 |
| `sync_hardcover.py` | Rewritten: C1, C3, C4, S2, R1–R3, R8, R9, E1, E2, E4, E5, H1, H3, H4, U1–U5, U7 |
| `tests/test_parse_goodreads.py` | New — 30 tests |
| `tests/test_sync_hardcover.py` | New — 51 tests |
| `pytest.ini` | New — test discovery config |
| `.github/workflows/tests.yml` | New — CI on Python 3.9–3.12 |
| `requirements.txt` | Security bump (P3) |
| `requirements-dev.txt` | New |
| `.gitignore` | P4 |
| `README.md` | P5, P6 |

### Compatibility

Existing invocations keep working unchanged — every new flag is optional and
every default matches the previous behaviour. Two intentional behaviour changes
are worth knowing about:

- Books whose dates already match on Hardcover are no longer rewritten (E1), so
  the "updated" count on a second run will be lower than before. That is the
  fix, not a regression.
- Books that previously failed to sync because they had no Date Read (C1) will
  now be updated, using the start date for both ends of the journey.
