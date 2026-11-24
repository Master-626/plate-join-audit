# plate-join-audit

Audit microplate CSV/TSV join keys before combining a plate layout with measurement exports.
Python 3.10+; no runtime dependencies. Experimental first release, not validated laboratory software.

## Why

Duplicate keys can multiply rows in a merge. A01 and a1 can refer to the same well, while
repeated measurements may legitimately differ by time or channel. This tool catches these
problems before a join; it never joins tables or changes the input files.

## Quick start

```sh
python -m pip install .
plate-join-audit examples/layout.csv examples/readings.csv
plate-join-audit examples/layout.csv examples/time-series.csv --observation-column time
plate-join-audit examples/layout.csv examples/readings.csv --output report.json
```

The CLI returns 0 for a passing audit, 1 for key/coverage issues, and 2 for invalid
input or output errors. JSON is printed to stdout unless --output is supplied.
Existing output files are never overwritten. File output uses an atomic hard link;
filesystems without hard-link support fail safely. Choose stdout on those filesystems.

```python
from plate_join_audit import audit, AuditError

try:
    report = audit("layout.csv", "readings.csv", observation_columns=("time",))
except AuditError as exc:
    print(f"Cannot audit: {exc}")
else:
    print(report["ok"], report["summary"]["noncanonical_rows"], report["issues"])
```

## Data contract

Both files must have a header and default columns `plate,well`. Other columns are
preserved in the input but ignored by the audit. Map column names using
--layout-plate, --layout-well, --readings-plate and --readings-well.
Use --layout-tsv / --readings-tsv for tabs. See --help for every option.

* Regular files only (no pipes/devices). UTF-8 (optional BOM), comma or tab delimited; each file at most 16 MiB.
  Python CSV parser field limits also apply (normally 131,072 characters per field).
  Oversized fields fail explicitly; the library does not change global parser settings.
* Duplicate/blank headers, malformed CSV, blank rows, ragged rows and NUL bytes fail.
  A blank row is an empty record, or one whose fields are all empty or whitespace
  (including a delimiter-only line such as `,,`). It is rejected before any verdict.
* Record references are 1-based **data record numbers**, not physical line numbers.
* Plate IDs are case-sensitive, nonempty, and cannot have surrounding whitespace.
* Well aliases A01/a1/ A1 normalize to A1. Only ASCII coordinates are accepted.
  **Your subsequent join must use the same normalization on both tables.** A passing
  audit does not make raw A01/a1 strings equal; use the exported normalize_well function.
  Rows whose raw token is not already canonical are listed in noncanonical_wells
  and counted as summary.noncanonical_rows. Those rows still match.
* Select one geometry for both files: 6, 12, 24, 48, 96 (default), or 384 wells.
  Partial layouts are supported; unlisted wells are unexpected, not implicitly blank.
* Layout keys (plate, canonical well) must be unique.
* Reading keys additionally include every explicit --observation-column, repeatable
  for time AND channel. Without these flags, repeated wells are reported as duplicates.
* Observation values are exact nonempty strings without surrounding whitespace.
  Numeric-looking "01" and "1" remain distinct; there is no timestamp/unit conversion.
* Every planned well must have a valid reading; every observed well must be planned.
* A passing report does **not** verify measurement values, sample identity, scientific
  correctness, time/channel completeness, plate orientation or instrument compatibility.

Schema v1 JSON includes SHA-256 hashes of the exact source bytes, input basenames,
options, summary counts, noncanonical well spellings and structured issues.
Reports may contain plate IDs and observation labels; review before sharing.
No network access or telemetry is used.

## Development

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check .
python -m build
```

## 中文

合并板图与酶标仪长表前，检查板位归一化冲突、重复观测、缺失和多余板位。
默认一孔一条读数；时间序列用 `--observation-column time` 显式指定唯一键，
多个维度可重复指定。只检查关联键及计划孔覆盖，不检查数值、实验结果或时间点完整性。
报告中的 noncanonical_wells 列出 A01、a1 这类尚未规范化的孔位写法，便于合并前统一；这些行仍视为同一孔。
输入文件只读，不自动修复。报告可能包含样本相关标识，分享前请检查。
不修改任何现有项目；本项目与 Wellmap 没有隶属关系。

## Design references and limits

* [Wellmap workflow](https://wellmap.readthedocs.io/en/latest/basic_usage_python.html)
  documents associating experimental layouts with measurements.
* [pandas merging guide](https://pandas.pydata.org/docs/user_guide/merging.html)
  describes duplicate-key row multiplication.

These demonstrate a workflow and failure mechanism, not evidence of users adopting
this project. Wellmap offers richer layout definition/visualization; this tool is a
small long-form CSV preflight, not a replacement. No scientific or commercial adoption
has been claimed.

MIT license. Maintained as an independent project.
