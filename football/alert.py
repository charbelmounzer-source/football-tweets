"""Turn a failed run's log into a short alert the owner can read on his phone.

    python -m football.alert --log run.log --run-url URL --out alert.md

alert.md: first line = issue title, the rest = issue body (markdown). The daily workflow opens
an issue with it (assigned to the owner, so GitHub emails him), comments on the open one if the
run fails again, and closes it when a run succeeds. Reading the log is all this does: no
network, so it works even when everything else is broken.
"""

import argparse
import re
from datetime import datetime, timezone

# (pattern in the log, short cause, what it means and what to do). First match wins, so the
# most specific causes come first.
CAUSES = [
    (r"HTTP 403|HTTP 429",
     "FotMob is refusing our requests",
     "FotMob answered 403 (forbidden) or 429 (too many requests): it may be blocking GitHub's "
     "servers. Nothing wrong was loaded. If it lasts several days: run the same command from "
     "another computer, or see \"If FotMob breaks\" in CLAUDE.md."),
    (r"not JSON|unreadable response",
     "FotMob changed what it sends",
     "FotMob answered, but not with the data we read (a web page, a block notice, a new "
     "format). Its website or API probably changed: the code needs updating (football/fotmob.py "
     "or football/model.py). Nothing wrong was loaded."),
    (r"stopped: FotMob is not answering|stopped: too many failed requests|network error|HTTP 5\d\d",
     "FotMob is not answering",
     "FotMob did not answer (down, or a network problem). Usually temporary: the next run "
     "retries by itself. Worry only if it lasts more than a day or two."),
    (r"answer is for league|answer is for season|no matches in the answer|FotMob now lists .* answer refused",
     "FotMob sent unexpected league data",
     "A league page came back empty, for another season, or with fewer results than we have. "
     "It was refused and the previous data kept. If it repeats, FotMob's league pages changed."),
    (r"data-quality error",
     "A data check failed",
     "The data was downloaded but a quality check found an error, so no tweets were drafted "
     "(nothing wrong is posted). The failing checks are listed below."),
    (r"google\.|bigquery|BigQuery|Forbidden: 403|NotFound: 404|BadRequest: 400|Unauthorized|"
     r"credentials|DefaultCredentialsError",
     "BigQuery problem",
     "Saving to or reading from BigQuery failed (permissions, billing, quota or a Google "
     "outage). FotMob is not the cause. Check the Google Cloud project and the GCP_SA_KEY secret."),
]
UNKNOWN = ("Unexpected error",
           "The run stopped on an error that has no known cause. The log lines below are the "
           "best clue; the full log is behind the link.")

CHECK_HELP = {
    "F1": "a league was not downloaded in the last 36 hours",
    "F2": "recent match pages suddenly lack stats they always had: FotMob probably changed its format",
    "F3": "many matches past kick-off still have no result: FotMob may have stopped updating results",
}


def interesting(lines):
    """The lines worth showing: errors, check failures, the end of a traceback."""
    keep = [x for x in lines if re.search(r"ERROR|Error|error|WARN|Traceback|stopped:|refused|"
                                          r"data-quality|Finished with", x)]
    return keep[-15:] or lines[-15:]


def describe(log, run_url=None, when=None):
    """-> (title, markdown body)"""
    when = when or datetime.now(timezone.utc).strftime("%d %b %Y")
    lines = [x.rstrip() for x in log.splitlines() if x.strip()]
    text = "\n".join(lines)
    cause, meaning = UNKNOWN
    for pattern, c, m in CAUSES:
        if re.search(pattern, text):
            cause, meaning = c, m
            break
    checks = re.findall(r"ERROR\s+([A-Z]\d)\s+(.*)", text)
    body = [f"**What happened:** {meaning}", ""]
    if checks:
        body.append("**Failing checks:**")
        for cid, desc in checks:
            hint = CHECK_HELP.get(cid)
            body.append(f"- `{cid}` {desc}" + (f" ({hint})" if hint else ""))
        body.append("")
    if not lines:
        body += ["The run stopped before the pipeline started (setting up Python, installing "
                 "packages or logging in to Google). The full log is behind the link.", ""]
    else:
        body += ["**Last relevant log lines:**", "```", *interesting(lines), "```", ""]
    if run_url:
        body.append(f"Full log: {run_url}")
    body += ["", "_Opened by the Daily update workflow. It closes itself when a run succeeds "
             "again; a new failure before that is added here as a comment._"]
    return f"Daily update failed ({when}): {cause}", "\n".join(body)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m football.alert")
    p.add_argument("--log", required=True)
    p.add_argument("--run-url")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    try:
        with open(args.log, encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError:
        log = ""
    title, body = describe(log, args.run_url)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(title + "\n" + body + "\n")


if __name__ == "__main__":
    main()
