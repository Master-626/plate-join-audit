import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plate_join_audit import AuditError, audit, normalize_well
from plate_join_audit.cli import main


def table(tmp_path, name, text):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


def pair(
    tmp_path, readings="plate,well,value\np,A1,1\n", layout="plate,well,group\np,A1,control\n"
):
    return table(tmp_path, "map.csv", layout), table(tmp_path, "data.csv", readings)


@pytest.mark.parametrize("raw,expected", [("a01", "A1"), (" A0001\t", "A1"), ("H12", "H12")])
def test_normalize(raw, expected):
    assert normalize_well(raw) == expected


@pytest.mark.parametrize(
    "bad", ["A0", "A-1", "AA1", "I1", "H13", "Ａ1", "A١", "1A", "", "A1.0", "A" + "1" * 5000]
)
def test_bad_well(bad):
    with pytest.raises(AuditError):
        normalize_well(bad)


@pytest.mark.parametrize(
    "size,last", [(6, "B3"), (12, "C4"), (24, "D6"), (48, "F8"), (96, "H12"), (384, "P24")]
)
def test_geometry(size, last):
    assert normalize_well(last, size) == last


@pytest.mark.parametrize("size", [True, 0, 1536, "96"])
def test_bad_geometry(size):
    with pytest.raises(AuditError):
        normalize_well("A1", size)


def test_clean_and_hash(tmp_path):
    a, b = pair(tmp_path, "plate,well,value\np,a01,1\n")
    before = (a.read_bytes(), b.read_bytes())
    r = audit(a, b)
    assert r["ok"] and r["summary"]["matched_unique_wells"] == 1
    assert r["sources"]["readings"]["sha256"] == hashlib.sha256(before[1]).hexdigest()
    assert before == (a.read_bytes(), b.read_bytes())


def test_duplicate_layout_alias(tmp_path):
    a, b = pair(tmp_path, layout="plate,well\np,A01\np,a1\n")
    r = audit(a, b)
    assert not r["ok"]
    assert r["issues"][0] == {
        "code": "duplicate_layout_well",
        "table": "layout",
        "records": [1, 2],
        "plate": "p",
        "well": "A1",
    }


def test_duplicate_readings(tmp_path):
    a, b = pair(tmp_path, "plate,well\np,A1\np,a01\n")
    assert audit(a, b)["summary"]["issues_by_code"] == {"duplicate_observation": 1}


def test_missing_unexpected(tmp_path):
    a, b = pair(tmp_path, "plate,well\np,A2\n")
    assert audit(a, b)["summary"]["issues_by_code"] == {"missing_well": 1, "unexpected_well": 1}


def test_multi_plate(tmp_path):
    a, b = pair(tmp_path, "plate,well\nx,A1\ny,A1\n", "plate,well\nx,A1\ny,A1\n")
    assert audit(a, b)["ok"]


def test_longitudinal(tmp_path):
    a, b = pair(tmp_path, "plate,well,time,channel\np,A1,0,red\np,a01,1,red\np,A01,0,green\n")
    assert not audit(a, b)["ok"]
    assert audit(a, b, observation_columns=("time", "channel"))["ok"]


def test_longitudinal_collision(tmp_path):
    a, b = pair(tmp_path, "plate,well,time\np,A1,01\np,A01,01\n")
    assert audit(a, b, observation_columns=("time",))["summary"]["issues_by_code"] == {
        "duplicate_observation": 1
    }


def test_text_time_is_not_numeric_normalized(tmp_path):
    a, b = pair(tmp_path, "plate,well,time\np,A1,01\np,A1,1\n")
    assert audit(a, b, observation_columns=("time",))["ok"]


def test_missing_observation_does_not_count_coverage(tmp_path):
    a, b = pair(tmp_path, "plate,well,time\np,A1,\n")
    assert audit(a, b, observation_columns=("time",))["summary"]["issues_by_code"] == {
        "invalid_observation": 1,
        "missing_well": 1,
    }


@pytest.mark.parametrize("text", ["plate,well\n p,A1\n", "plate,well\n,A1\n", "plate,well\np,I1\n"])
def test_invalid_row(tmp_path, text):
    a, b = pair(tmp_path, text)
    assert not audit(a, b)["ok"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plate,plate,well\n",
        ",well\n",
        "plate,well\np,A1,extra\n",
        "plate,well\np\n",
        'plate,well\n"unterminated',
        "plate,well\np,A1\x00\n",
    ],
)
def test_bad_file(tmp_path, text):
    a, b = pair(tmp_path, text)
    with pytest.raises(AuditError):
        audit(a, b)


def test_empty_tables_not_pass(tmp_path):
    a, b = pair(tmp_path, "plate,well\n", "plate,well\n")
    r = audit(a, b)
    assert not r["ok"] and r["summary"]["issues_by_code"] == {"empty_table": 2}


def test_tsv_bom_unicode_and_column_mapping(tmp_path):
    a = table(tmp_path, "map.tsv", "\ufeff板\t孔\n一\tA1\n")
    b = table(tmp_path, "data.tsv", "plate\tposition\treading\n一\ta01\t0\n")
    assert audit(
        a,
        b,
        layout_plate="板",
        layout_well="孔",
        readings_well="position",
        layout_delimiter="\t",
        readings_delimiter="\t",
    )["ok"]


def test_crlf_and_quoted_newline(tmp_path):
    a, b = pair(tmp_path, 'plate,well,note\r\np,A1,"a\r\nb"\r\n')
    assert audit(a, b)["ok"]


def test_missing_file(tmp_path):
    with pytest.raises(AuditError):
        audit(tmp_path / "missing", tmp_path / "missing2")


def test_invalid_encoding(tmp_path):
    a, b = pair(tmp_path)
    b.write_bytes(b"\xff")
    with pytest.raises(AuditError):
        audit(a, b)


def test_file_limit(tmp_path, monkeypatch):
    from plate_join_audit import core

    a, b = pair(tmp_path)
    monkeypatch.setattr(core, "MAX_BYTES", 3)
    with pytest.raises(AuditError):
        audit(a, b)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observation_columns": "time"},
        {"observation_columns": ("well",)},
        {"observation_columns": ("time", "time")},
        {"layout_well": "plate"},
        {"layout_delimiter": ";"},
    ],
)
def test_invalid_options(tmp_path, kwargs):
    a, b = pair(tmp_path)
    with pytest.raises(AuditError):
        audit(a, b, **kwargs)


def test_cli_json_and_exit(tmp_path, capsys):
    a, b = pair(tmp_path)
    assert main([str(a), str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    b.write_text("plate,well\np,A2\n")
    assert main([str(a), str(b)]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert main([str(a), "missing"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_no_overwrite_or_source_clobber(tmp_path, capsys):
    a, b = pair(tmp_path)
    original = a.read_bytes()
    assert main([str(a), str(b), "--output", str(a)]) == 2
    assert a.read_bytes() == original
    p = tmp_path / "report.json"
    assert main([str(a), str(b), "--output", str(p)]) == 0
    assert json.loads(p.read_text())["ok"]
    original = p.read_bytes()
    assert main([str(a), str(b), "--output", str(p)]) == 2
    assert p.read_bytes() == original
    assert sorted(x.name for x in tmp_path.iterdir()) == ["data.csv", "map.csv", "report.json"]


def test_symlink_refused(tmp_path):
    a, b = pair(tmp_path)
    link = tmp_path / "report.json"
    try:
        link.symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("OS does not allow symlink creation")
    assert main([str(a), str(b), "--output", str(link)]) == 2
    assert link.is_symlink() and not (tmp_path / "missing").exists()


def test_link_unavailable(tmp_path, monkeypatch):
    from plate_join_audit import cli

    a, b = pair(tmp_path)

    def fail(*args):
        raise OSError("no hard links")

    monkeypatch.setattr(cli.os, "link", fail)
    assert main([str(a), str(b), "--output", str(tmp_path / "out")]) == 2
    assert sorted(x.name for x in tmp_path.iterdir()) == ["data.csv", "map.csv"]


def test_module_subprocess(tmp_path):
    a, b = pair(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "plate_join_audit", str(a), str(b)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["ok"]


def test_parser_field_limit(tmp_path):
    import csv

    a, b = pair(tmp_path)
    b.write_text("plate,well,note\np1,A1," + "x" * (csv.field_size_limit() + 1) + "\n")
    with pytest.raises(AuditError, match="field larger than field limit"):
        audit(a, b)


@pytest.mark.parametrize(
    "options", [{"layout_plate": []}, {"layout_well": None}, {"observation_columns": None}]
)
def test_invalid_api_column_types(tmp_path, options):
    a, b = pair(tmp_path)
    with pytest.raises(AuditError):
        audit(a, b, **options)


def test_fifo_rejected_without_blocking(tmp_path):
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    _a, b = pair(tmp_path)
    fifo = tmp_path / "pipe.csv"
    os.mkfifo(fifo)
    result = subprocess.run(
        [sys.executable, "-m", "plate_join_audit", str(fifo), str(b)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "only regular files" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "plate,well\n,\n",
        "plate,well\n , \n",
        'plate,well\n"",""\n',
        "plate,well\np,A1\n\n",
        "plate,well,note\n,,\n",
        "plate\twell\n\t\n",
    ],
)
def test_blank_rows_are_invalid_input(tmp_path, text):
    delimiter = "\t" if "\t" in text else ","
    layout, readings = pair(tmp_path)
    readings.write_text(text)
    with pytest.raises(AuditError, match="blank row"):
        audit(layout, readings, readings_delimiter=delimiter)


def test_blank_row_cli_exits_without_a_verdict(tmp_path, capsys):
    layout, readings = pair(tmp_path, "plate,well\n,\n")
    out = tmp_path / "out.json"
    assert main([str(layout), str(readings), "--output", str(out)]) == 2
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "blank row" in captured.err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data.csv", "map.csv"]


def test_partial_or_optional_empty_fields_are_not_blank_rows(tmp_path):
    layout, readings = pair(tmp_path, "plate,well\n,A1\n")
    report = audit(layout, readings)
    assert report["summary"]["issues_by_code"]["invalid_plate"] == 1
    layout, readings = pair(tmp_path, "plate,well,note\np,A1,\n")
    assert audit(layout, readings)["ok"]
    readings.write_text("plate,well\np\n")
    with pytest.raises(AuditError, match="wrong number of columns"):
        audit(layout, readings)


def test_duplicate_observations_use_numeric_well_order(tmp_path):
    layout, readings = pair(
        tmp_path,
        "plate,well,time\n"
        "p,A10,2\n"
        "p,A10,2\n"
        "p,A2,10\n"
        "p,A2,10\n"
        "p,A2,2\n"
        "p,A2,2\n"
        "p,A1,2\n"
        "p,A1,2\n",
        "plate,well\np,A1\np,A2\np,A10\n",
    )
    issues = [
        item
        for item in audit(layout, readings, observation_columns=("time",))["issues"]
        if item["code"] == "duplicate_observation"
    ]
    assert [(item["well"], item["observation"]["time"]) for item in issues] == [
        ("A1", "2"),
        ("A2", "10"),
        ("A2", "2"),
        ("A10", "2"),
    ]


def test_short_reads_hash_the_whole_file(tmp_path, monkeypatch):
    from plate_join_audit import core

    layout, readings = pair(
        tmp_path,
        "plate,well,value\np,A1,abcdefghij\np,A2,klmnopqrst\n",
        "plate,well,group\np,A1,control\np,A2,case\n",
    )
    real_fdopen = os.fdopen

    def fdopen(fd, mode="rb", *args, **kwargs):
        stream = real_fdopen(fd, mode, *args, **kwargs)
        original = stream.read

        def short(n=-1):
            if n is None or n < 0:
                return original()
            return original(min(int(n), 7))

        stream.read = short
        return stream

    monkeypatch.setattr(core.os, "fdopen", fdopen)
    report = audit(layout, readings)
    assert report["ok"]
    payload = readings.read_bytes()
    assert report["sources"]["readings"]["bytes"] == len(payload)
    assert report["sources"]["readings"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["summary"]["matched_unique_wells"] == 2


def test_noncanonical_spellings_stay_matches(tmp_path):
    layout, readings = pair(
        tmp_path,
        "plate,well\nm,A1\np,A2\np,A10\n",
        "plate,well\np,a02\np,A010\nm,A1\n",
    )
    report = audit(layout, readings)
    assert report["ok"]
    assert report["noncanonical_wells"] == [
        {
            "table": "layout",
            "plate": "p",
            "well": "A2",
            "raw_well": "a02",
            "records": [1],
        },
        {
            "table": "layout",
            "plate": "p",
            "well": "A10",
            "raw_well": "A010",
            "records": [2],
        },
    ]
    assert report["summary"]["noncanonical_rows"] == 2


def test_noncanonical_spellings_group_rows_and_sort_tables(tmp_path):
    layout, readings = pair(
        tmp_path,
        "plate,well\nz,A01\nz,a1\nz,A01\n",
        "plate,well\nz,A1\n",
    )
    report = audit(layout, readings)
    assert report["noncanonical_wells"] == [
        {
            "table": "readings",
            "plate": "z",
            "well": "A1",
            "raw_well": "A01",
            "records": [1, 3],
        },
        {
            "table": "readings",
            "plate": "z",
            "well": "A1",
            "raw_well": "a1",
            "records": [2],
        },
    ]
    assert report["summary"]["noncanonical_rows"] == 3
    assert report["summary"]["issues_by_code"]["duplicate_observation"] == 1

    layout, readings = pair(tmp_path, "plate,well\na,a1\n", "plate,well\nz,A01\n")
    report = audit(layout, readings)
    assert [(item["table"], item["plate"]) for item in report["noncanonical_wells"]] == [
        ("layout", "z"),
        ("readings", "a"),
    ]


def test_example_files_list_real_aliases():
    root = Path(__file__).resolve().parents[1]
    report = audit(root / "examples" / "layout.csv", root / "examples" / "readings.csv")
    assert report["ok"]
    assert report["noncanonical_wells"] == [
        {"table": "layout", "plate": "p1", "well": "A1", "raw_well": "A01", "records": [1]},
        {"table": "layout", "plate": "p1", "well": "A2", "raw_well": "A02", "records": [2]},
        {"table": "readings", "plate": "p1", "well": "A1", "raw_well": "a1", "records": [1]},
    ]
    assert report["summary"]["noncanonical_rows"] == 3
    series = audit(
        root / "examples" / "layout.csv",
        root / "examples" / "time-series.csv",
        observation_columns=("time",),
    )
    assert series["ok"]
    assert [item["raw_well"] for item in series["noncanonical_wells"]] == ["A01", "A02"]
    assert all(item["table"] == "layout" for item in series["noncanonical_wells"])
