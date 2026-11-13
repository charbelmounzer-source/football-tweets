from football import alert

URL = "https://github.com/o/r/actions/runs/1"


def test_fotmob_blocking_is_named_in_plain_words():
    log = """1/3 update
  ERROR league Premier League 2026/27: leagues?id=47: HTTP 403
  ERROR league La Liga 2026/27: leagues?id=87: HTTP 403
Finished with 2 download error(s) (see core.runs)"""
    title, body = alert.describe(log, URL, when="14 Nov 2026")
    assert title == "Daily update failed (14 Nov 2026): FotMob is refusing our requests"
    assert "blocking GitHub's servers" in body and "HTTP 403" in body and URL in body


def test_a_changed_format_is_told_apart_from_an_outage():
    changed = "  ERROR match 123: matchDetails?matchId=123: unreadable response: not JSON - HTTP 200, text/html"
    down = "  ERROR league Serie A 2026/27: leagues?id=55: HTTP 503\n  stopped: FotMob is not answering"
    assert "FotMob changed what it sends" in alert.describe(changed)[0]
    assert "FotMob is not answering" in alert.describe(down)[0]


def test_failed_checks_are_listed_with_what_they_mean():
    log = """2/3 checks
  ERROR F2 Recent matches without stats FotMob always gives: 1 (Premier League: 9/10 without xG)
  checks: 16 run, 1 errors, 0 warnings
1 data-quality error(s): no tweets drafted. See core.latest_checks."""
    title, body = alert.describe(log)
    assert "A data check failed" in title
    assert "`F2` Recent matches" in body and "probably changed its format" in body


def test_bigquery_and_unknown_errors_and_an_empty_log(tmp_path):
    bq = "google.api_core.exceptions.Forbidden: 403 Access Denied: Table football_core.matches"
    assert "BigQuery problem" in alert.describe(bq)[0]
    assert "Unexpected error" in alert.describe("Traceback (most recent call last):\nKeyError: 'x'")[0]
    out = tmp_path / "alert.md"
    alert.main(["--log", str(tmp_path / "missing.log"), "--out", str(out), "--run-url", URL])
    text = out.read_text(encoding="utf-8")
    assert text.startswith("Daily update failed") and "before the pipeline started" in text
