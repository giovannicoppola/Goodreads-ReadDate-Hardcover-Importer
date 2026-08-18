"""Tests for parse_goodreads.py."""

import json

import pytest
from bs4 import BeautifulSoup

import parse_goodreads as pg


def make_row(html):
    return BeautifulSoup(html, "html.parser").find("tr")


FULL_ROW = """
<tr class="bookalike review">
  <td class="field title"><a>Some Book</a></td>
  <td class="field author"><a>Doe, Jane</a></td>
  <td class="field date_started"><span class="date_started_value">Jan 02, 2024</span></td>
  <td class="field date_read"><span class="date_read_value">Feb 03, 2024</span></td>
  <td class="field avg_rating">4.10</td>
  <td class="field num_pages"><nobr>320 pp</nobr></td>
  <div class="js-tooltipTrigger" data-resource-id="12345"></div>
</tr>
"""


class TestConvertDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Jan 02, 2024", "2024-01-02"),
            ("January 02, 2024", "2024-01-02"),
            ("Mar 2020", "2020-03-01"),
            ("March 2020", "2020-03-01"),
            ("not set", ""),
            ("Not Set", ""),
            ("", ""),
            (None, ""),
            ("  Jan 02, 2024  ", "2024-01-02"),
        ],
    )
    def test_conversions(self, raw, expected):
        assert pg.convert_date(raw) == expected

    def test_unparseable_date_is_passed_through(self):
        assert pg.convert_date("someday") == "someday"


class TestProcessReviewRow:
    def test_full_row(self):
        assert pg.process_review_row(make_row(FULL_ROW)) == {
            "Goodreads ID": "12345",
            "Book Title": "Some Book",
            "Date Started": "2024-01-02",
            "Date Read": "2024-02-03",
            "Author": "Doe, Jane",
            "Average Rating": "4.10",
            "Number of Pages": "320",
        }

    def test_missing_date_read_column_does_not_raise(self):
        html = FULL_ROW.replace(
            '<td class="field date_read"><span class="date_read_value">Feb 03, 2024</span></td>', ""
        )
        assert pg.process_review_row(make_row(html))["Date Read"] == ""

    def test_missing_num_pages_column_does_not_raise(self):
        html = FULL_ROW.replace('<td class="field num_pages"><nobr>320 pp</nobr></td>', "")
        assert pg.process_review_row(make_row(html))["Number of Pages"] == ""

    def test_missing_tooltip_yields_empty_id(self):
        html = FULL_ROW.replace('<div class="js-tooltipTrigger" data-resource-id="12345"></div>', "")
        assert pg.process_review_row(make_row(html))["Goodreads ID"] == ""

    def test_empty_row_yields_all_blanks(self):
        row = make_row('<tr class="bookalike review"></tr>')
        assert set(pg.process_review_row(row).values()) == {""}


class TestFindTotalPages:
    def test_reads_highest_page_number(self):
        html = """
        <div id="reviewPagination">
          <em class="current">1</em><a href="#">2</a><a href="#">3</a>
          <a href="#">11</a><a class="next_page" href="#">next &raquo;</a>
        </div>
        """
        assert pg.find_total_pages(BeautifulSoup(html, "html.parser")) == 11

    def test_no_pagination_widget(self):
        assert pg.find_total_pages(BeautifulSoup("<div></div>", "html.parser")) is None

    def test_pagination_without_numeric_links(self):
        html = '<div id="reviewPagination"><a href="#">next &raquo;</a></div>'
        assert pg.find_total_pages(BeautifulSoup(html, "html.parser")) is None


class TestLoadCookies:
    def test_missing_file_returns_none(self, tmp_path):
        assert pg.load_cookies(str(tmp_path / "nope.json")) is None

    def test_invalid_json_returns_none(self, tmp_path):
        p = tmp_path / "cookies.json"
        p.write_text("not json")
        assert pg.load_cookies(str(p)) is None

    def test_only_goodreads_cookies_are_kept(self, tmp_path):
        p = tmp_path / "cookies.json"
        p.write_text(
            json.dumps(
                [
                    {"name": "gr_session", "value": "a", "domain": ".goodreads.com"},
                    {"name": "csid", "value": "b", "domain": "www.goodreads.com"},
                    # These must never be forwarded to Goodreads.
                    {"name": "fb_token", "value": "c", "domain": ".facebook.com", "hostOnly": False},
                    {"name": "bank", "value": "d", "domain": "bank.example", "hostOnly": False},
                ]
            )
        )
        assert pg.load_cookies(str(p)) == {"gr_session": "a", "csid": "b"}

    def test_no_goodreads_cookies_returns_none(self, tmp_path):
        p = tmp_path / "cookies.json"
        p.write_text(json.dumps([{"name": "x", "value": "y", "domain": ".example.com"}]))
        assert pg.load_cookies(str(p)) is None

    def test_malformed_entries_are_ignored(self, tmp_path):
        p = tmp_path / "cookies.json"
        p.write_text(json.dumps([{"domain": ".goodreads.com"}, "junk", {"name": "ok", "value": "1", "domain": "goodreads.com"}]))
        assert pg.load_cookies(str(p)) == {"ok": "1"}


class TestWarnIfNoReadDates:
    def test_warns_when_all_read_dates_blank(self, tmp_path, capsys):
        p = tmp_path / "books.tsv"
        p.write_text("Goodreads ID\tBook Title\tDate Read\n1\tA\t\n2\tB\t\n")
        pg.warn_if_no_read_dates(str(p))
        assert "Date Read" in capsys.readouterr().out

    def test_silent_when_a_read_date_exists(self, tmp_path, capsys):
        p = tmp_path / "books.tsv"
        p.write_text("Goodreads ID\tBook Title\tDate Read\n1\tA\t2024-01-01\n")
        pg.warn_if_no_read_dates(str(p))
        assert capsys.readouterr().out == ""


PAGE_TEMPLATE = """<html><body>
<div id="reviewPagination"><em>1</em><a href="#">2</a><a class="next_page" href="#">next</a></div>
<table>{rows}</table></body></html>"""

BOOK_ROW = """
<tr class="bookalike review">
  <td class="field title"><a>Book {i}</a></td>
  <td class="field author"><a>Author {i}</a></td>
  <td class="field date_started"><span class="date_started_value">Jan 0{i}, 2024</span></td>
  <td class="field date_read"><span class="date_read_value">not set</span></td>
  <td class="field avg_rating">4.0{i}</td>
  <td class="field num_pages"><nobr>3{i}0 pp</nobr></td>
  <div class="js-tooltipTrigger" data-resource-id="100{i}"></div>
</tr>"""

EMPTY_ROW = '<tr class="bookalike review"></tr>'


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


class TestDownloadAndProcess:
    def _run(self, monkeypatch, tmp_path):
        calls = []

        def fake_get(self, url, timeout=None):
            calls.append(url)
            page = 1 if "?page=1&" in url else 2
            return _FakeResponse(PAGE_TEMPLATE.format(rows=BOOK_ROW.format(i=page) + EMPTY_ROW))

        monkeypatch.setattr(pg.requests.Session, "get", fake_get)
        out = tmp_path / "books.tsv"
        ok = pg.download_and_process_goodreads_data(
            "1-user", str(out), cookies_file=str(tmp_path / "none.json"), delay=0
        )
        return ok, out, calls

    def test_writes_all_pages(self, monkeypatch, tmp_path):
        ok, out, calls = self._run(monkeypatch, tmp_path)
        assert ok
        lines = out.read_text().strip().splitlines()
        assert lines[0].split("\t") == pg.FIELDNAMES
        assert [line.split("\t")[0] for line in lines[1:]] == ["1001", "1002"]

    def test_first_page_is_fetched_only_once(self, monkeypatch, tmp_path):
        _ok, _out, calls = self._run(monkeypatch, tmp_path)
        assert sum("?page=1&" in url for url in calls) == 1

    def test_blank_rows_are_not_written(self, monkeypatch, tmp_path):
        _ok, out, _calls = self._run(monkeypatch, tmp_path)
        assert "\n\t" not in out.read_text()

    def test_not_set_becomes_empty(self, monkeypatch, tmp_path):
        _ok, out, _calls = self._run(monkeypatch, tmp_path)
        assert "not set" not in out.read_text()

    def test_existing_output_survives_a_failed_fetch(self, monkeypatch, tmp_path):
        out = tmp_path / "books.tsv"
        out.write_text("previous good export\n")

        def boom(self, url, timeout=None):
            raise pg.requests.exceptions.ConnectionError("no network")

        monkeypatch.setattr(pg.requests.Session, "get", boom)
        assert (
            pg.download_and_process_goodreads_data(
                "1-user", str(out), cookies_file=str(tmp_path / "none.json"), delay=0
            )
            is False
        )
        assert out.read_text() == "previous good export\n"
