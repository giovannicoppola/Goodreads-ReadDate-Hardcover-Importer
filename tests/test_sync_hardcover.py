"""Tests for sync_hardcover.py."""

import json
from unittest import mock

import pytest
import requests

import sync_hardcover as sh


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def logger():
    return sh.Logger(log_to_file=False)


@pytest.fixture
def client(logger):
    c = sh.HardcoverClient("Bearer token", logger=logger)
    c.base_delay = 0  # keep retry tests fast
    c.user_id = 42
    return c


def mapping_payload(journeys):
    return {
        "data": {
            "book_mappings": [
                {"id": 1, "book_id": 2, "platform_id": 1,
                 "book": {"user_books": [{"id": 9, "user_book_reads": journeys}]}}
            ]
        }
    }


class TestNormalizeApiKey:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("abc123", "Bearer abc123"),
            ("Bearer abc123", "Bearer abc123"),
            ("bearer abc123", "bearer abc123"),
            ("  abc123  ", "Bearer abc123"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_prefix(self, raw, expected):
        assert sh.normalize_api_key(raw) == expected


class TestLoadApiKey:
    def test_missing_file(self, tmp_path, logger):
        assert sh.load_api_key(str(tmp_path / "nope.json"), logger) is None

    def test_bad_json(self, tmp_path, logger):
        p = tmp_path / "k.json"
        p.write_text("{")
        assert sh.load_api_key(str(p), logger) is None

    def test_empty_key(self, tmp_path, logger):
        p = tmp_path / "k.json"
        p.write_text(json.dumps({"api_key": "   "}))
        assert sh.load_api_key(str(p), logger) is None

    def test_valid_key(self, tmp_path, logger):
        p = tmp_path / "k.json"
        p.write_text(json.dumps({"api_key": "abc"}))
        assert sh.load_api_key(str(p), logger) == "abc"


class TestFormatDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2024-01-02", "2024-01-02"),
            ("01/02/24", "2024-01-02"),
            ("01/02/2024", "2024-01-02"),
            ("Jan 02, 2024", "2024-01-02"),
            ("Mar 2020", "2020-03-01"),
            ("not set", None),
            ("", None),
            (None, None),
            ("gibberish", None),
        ],
    )
    def test_formats(self, raw, expected, logger):
        assert sh.format_date(raw, logger) == expected


class TestRequestRetry:
    def test_retries_then_succeeds(self, client):
        responses = [FakeResponse(status_code=504, text="gw"), FakeResponse({"data": {"ok": 1}})]
        with mock.patch.object(sh.requests, "post", side_effect=responses) as post:
            payload, ok = client._make_request_with_retry("query {}")
        assert ok and payload == {"data": {"ok": 1}}
        assert post.call_count == 2

    def test_gives_up_after_max_retries(self, client):
        with mock.patch.object(
            sh.requests, "post", return_value=FakeResponse(status_code=503, text="down")
        ) as post:
            payload, ok = client._make_request_with_retry("query {}")
        assert (payload, ok) == (None, False)
        assert post.call_count == client.max_retries

    def test_network_errors_are_retried(self, client):
        side = [requests.exceptions.ConnectionError("boom"), FakeResponse({"data": {}})]
        with mock.patch.object(sh.requests, "post", side_effect=side) as post:
            _payload, ok = client._make_request_with_retry("query {}")
        assert ok and post.call_count == 2

    def test_request_uses_a_timeout(self, client):
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse({"data": {}})) as post:
            client._make_request_with_retry("query {}")
        assert post.call_args.kwargs["timeout"] == client.timeout

    def test_graphql_errors_fail_the_request(self, client):
        payload = {"errors": [{"message": "unauthorized"}], "data": None}
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(payload)):
            assert client._make_request_with_retry("query {}") == (None, False)

    def test_non_json_body_fails_cleanly(self, client):
        with mock.patch.object(
            sh.requests, "post", return_value=FakeResponse(None, text="<html>oops</html>")
        ):
            assert client._make_request_with_retry("query {}") == (None, False)

    def test_honours_retry_after_on_429(self, client):
        client.base_delay = 1
        responses = [
            FakeResponse(status_code=429, text="slow down", headers={"Retry-After": "7"}),
            FakeResponse({"data": {}}),
        ]
        with mock.patch.object(sh.requests, "post", side_effect=responses), mock.patch.object(
            sh.time, "sleep"
        ) as sleep:
            client._make_request_with_retry("query {}")
        assert sleep.call_args[0][0] == 7


class TestGetUserId:
    def test_parses_user_id(self, logger):
        c = sh.HardcoverClient("k", logger=logger)
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse({"data": {"me": [{"id": 7}]}})):
            assert c.get_user_id() == 7

    def test_empty_me_returns_none(self, logger):
        c = sh.HardcoverClient("k", logger=logger)
        c.base_delay = 0
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse({"data": {"me": []}})):
            assert c.get_user_id() is None

    def test_user_id_is_cached(self, logger):
        c = sh.HardcoverClient("k", logger=logger)
        with mock.patch.object(
            sh.requests, "post", return_value=FakeResponse({"data": {"me": [{"id": 7}]}})
        ) as post:
            c.get_user_id()
            c.get_user_id()
        assert post.call_count == 1


class TestFindBookByGoodreadsId:
    def test_single_journey(self, client):
        payload = mapping_payload([{"id": 11, "started_at": "2020-01-01", "finished_at": "2020-02-01"}])
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(payload)):
            assert client.find_book_by_goodreads_id("123") == {
                "journey_id": 11, "started_at": "2020-01-01", "finished_at": "2020-02-01"
            }

    def test_no_mapping_returns_none_without_error(self, client, capsys):
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse({"data": {"book_mappings": []}})):
            assert client.find_book_by_goodreads_id("123") is None
        assert "index out of range" not in capsys.readouterr().out

    def test_book_not_on_shelf_returns_none(self, client, capsys):
        payload = {"data": {"book_mappings": [{"book": {"user_books": []}}]}}
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(payload)):
            assert client.find_book_by_goodreads_id("123") is None
        assert "index out of range" not in capsys.readouterr().out

    def test_no_journeys_returns_none(self, client):
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(mapping_payload([]))):
            assert client.find_book_by_goodreads_id("123") is None

    def test_multiple_journeys_prefers_matching_finish_date(self, client):
        payload = mapping_payload([
            {"id": 11, "started_at": "2020-01-01", "finished_at": "2020-02-01"},
            {"id": 12, "started_at": "2021-01-01", "finished_at": "2021-02-01"},
        ])
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(payload)):
            got = client.find_book_by_goodreads_id("123", "2021-02-01")
        assert got["journey_id"] == 12

    def test_multiple_journeys_falls_back_to_first(self, client):
        """README documents this fallback; it used to return None instead."""
        payload = mapping_payload([
            {"id": 11, "started_at": "2020-01-01", "finished_at": "2020-02-01"},
            {"id": 12, "started_at": "2021-01-01", "finished_at": "2021-02-01"},
        ])
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(payload)):
            got = client.find_book_by_goodreads_id("123", "1999-01-01")
        assert got["journey_id"] == 11


class TestUpdateReadingDate:
    def test_sends_expected_variables(self, client):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json)
            return FakeResponse({"data": {"update_user_book_read": {"id": 5}}})

        with mock.patch.object(sh.requests, "post", side_effect=fake_post):
            assert client.update_reading_date(5, "2024-01-02", "2024-02-03") is True
        assert captured["variables"] == {
            "journeyId": 5, "dateStarted": "2024-01-02", "dateFinished": "2024-02-03"
        }

    def test_dry_run_makes_no_request(self, logger):
        c = sh.HardcoverClient("k", logger=logger, dry_run=True)
        with mock.patch.object(sh.requests, "post") as post:
            assert c.update_reading_date(5, "2024-01-02", "2024-02-03") is True
        post.assert_not_called()

    def test_graphql_error_reports_failure(self, client):
        payload = {"errors": [{"message": "nope"}]}
        with mock.patch.object(sh.requests, "post", return_value=FakeResponse(payload)):
            assert client.update_reading_date(5, "2024-01-02", "2024-02-03") is False


HEADER = "Goodreads ID\tBook Title\tDate Started\tDate Read\n"


def write_tsv(tmp_path, body, header=HEADER):
    p = tmp_path / "books.tsv"
    p.write_text(header + body, encoding="utf-8")
    return str(p)


@pytest.fixture
def stub_client(logger):
    c = sh.HardcoverClient("k", logger=logger)
    c.user_id = 1
    c.find_book_by_goodreads_id = mock.Mock(
        return_value={"journey_id": 11, "started_at": None, "finished_at": None}
    )
    c.update_reading_date = mock.Mock(return_value=True)
    return c


class TestProcessTsvFile:
    def test_updates_a_book(self, tmp_path, stub_client):
        tsv = write_tsv(tmp_path, "123\tA Book\t2024-01-02\t2024-02-03\n")
        assert sh.process_tsv_file(tsv, stub_client, delay=0) == 0
        stub_client.update_reading_date.assert_called_once_with(11, "2024-01-02", "2024-02-03")

    def test_missing_finish_date_falls_back_to_start(self, tmp_path, stub_client):
        """Previously sent dateFinished=null into a non-nullable GraphQL field."""
        tsv = write_tsv(tmp_path, "123\tA Book\t2024-01-02\t\n")
        sh.process_tsv_file(tsv, stub_client, delay=0)
        stub_client.update_reading_date.assert_called_once_with(11, "2024-01-02", "2024-01-02")

    def test_missing_start_date_falls_back_to_finish(self, tmp_path, stub_client):
        tsv = write_tsv(tmp_path, "123\tA Book\t\t2024-02-03\n")
        sh.process_tsv_file(tsv, stub_client, delay=0)
        stub_client.update_reading_date.assert_called_once_with(11, "2024-02-03", "2024-02-03")

    def test_row_without_any_date_is_skipped(self, tmp_path, stub_client):
        tsv = write_tsv(tmp_path, "123\tA Book\t\t\n")
        sh.process_tsv_file(tsv, stub_client, delay=0)
        stub_client.update_reading_date.assert_not_called()

    def test_row_without_goodreads_id_is_skipped(self, tmp_path, stub_client):
        tsv = write_tsv(tmp_path, "\tA Book\t2024-01-02\t2024-02-03\n")
        sh.process_tsv_file(tsv, stub_client, delay=0)
        stub_client.update_reading_date.assert_not_called()

    def test_reversed_dates_are_skipped(self, tmp_path, stub_client):
        tsv = write_tsv(tmp_path, "123\tA Book\t2024-05-02\t2024-02-03\n")
        sh.process_tsv_file(tsv, stub_client, delay=0)
        stub_client.update_reading_date.assert_not_called()

    def test_unchanged_dates_skip_the_mutation(self, tmp_path, stub_client):
        stub_client.find_book_by_goodreads_id.return_value = {
            "journey_id": 11, "started_at": "2024-01-02", "finished_at": "2024-02-03"
        }
        tsv = write_tsv(tmp_path, "123\tA Book\t2024-01-02\t2024-02-03\n")
        sh.process_tsv_file(tsv, stub_client, delay=0)
        stub_client.update_reading_date.assert_not_called()

    def test_limit_stops_early(self, tmp_path, stub_client):
        body = "".join(f"{i}\tBook {i}\t2024-01-02\t2024-02-03\n" for i in range(10))
        tsv = write_tsv(tmp_path, body)
        sh.process_tsv_file(tsv, stub_client, limit=3, delay=0)
        assert stub_client.update_reading_date.call_count == 3

    def test_missing_columns_are_reported(self, tmp_path, stub_client, capsys):
        tsv = write_tsv(tmp_path, "123\tA Book\n", header="Goodreads ID\tBook Title\n")
        assert sh.process_tsv_file(tsv, stub_client, delay=0) == 1
        assert "missing required column" in capsys.readouterr().out

    def test_missing_file_is_reported(self, tmp_path, stub_client, capsys):
        assert sh.process_tsv_file(str(tmp_path / "nope.tsv"), stub_client, delay=0) == 1
        assert "not found" in capsys.readouterr().out

    def test_failed_update_sets_nonzero_exit(self, tmp_path, stub_client):
        stub_client.update_reading_date.return_value = False
        tsv = write_tsv(tmp_path, "123\tA Book\t2024-01-02\t2024-02-03\n")
        assert sh.process_tsv_file(tsv, stub_client, delay=0) == 1


class TestLogger:
    def test_closes_on_context_exit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with sh.Logger(log_to_file=True) as log:
            log.log("hello")
        content = (tmp_path / "log.txt").read_text()
        assert "hello" in content and "Log completed at" in content

    def test_closes_even_when_body_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError):
            with sh.Logger(log_to_file=True) as log:
                log.log("before the crash")
                raise RuntimeError("boom")
        assert "Log completed at" in (tmp_path / "log.txt").read_text()
