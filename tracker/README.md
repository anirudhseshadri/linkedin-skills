# Engagement tracker

Keeps a record of who reacts to and comments on a LinkedIn profile's posts, across
every scrape, and turns it into the **Post Pulse** dashboard: posts, audience mix,
comment threads, repeat engagers, ICP prospects, and growth per post over time.

Standard library only (SQLite). No accounts or keys: it reads files you export.

## Where the data comes from

Run the Apify actor **LinkedIn Profile Posts Scraper (No Cookies)** on a profile with
reactions and comments turned on, then download the dataset as JSON. That file is a
flat list of `post`, `reaction` and `comment` records, which is what this imports.

## Use

From the repo root:

```bash
python3 -m tracker import ~/Downloads/dataset.json    # add a scrape, rebuild the dashboard
open tracker/data/post-pulse.html                     # macOS; any browser works

python3 -m tracker prospects                          # ICP prospects, strongest signal first
python3 -m tracker prospects --csv prospects.csv
python3 -m tracker mark "Jane Doe" contacted --note "DM sent 26 Sep"
python3 -m tracker summary
```

Import the next scrape of the same profile a few days later. The tracker keeps what
it already has, adds only new people, reactions and comments, and stores that day's
counts for every post. The dashboard then shows who is **new since the last import**
and how each post grew. Importing the exact same file twice is refused, so the
history is never padded with a fake snapshot.

Statuses for `mark`: `open`, `contacted`, `replied`, `meeting`, `customer`, `not-a-fit`.

## What counts as a prospect

`tracker/config.json` holds the rules. They read each person's current LinkedIn
headline (past jobs such as "Ex-GEP" are ignored):

- **Team:** headline mentions one of `team_keywords`.
- **Prospect (ICP):** a procurement function word, a seniority word, and none of the
  exclude words (consultancies, vendors, students).
- **Signal score:** asked to talk in a comment (+4), commented (+3), each extra post
  engaged (+2), new since last import (+1).

To change the rules without touching the committed defaults, copy the file to
`tracker/data/config.json` and edit that one.

## Files

| Path | What it is |
|---|---|
| `store.py` | Database schema and the importer |
| `classify.py` | Audience groups and ICP rules |
| `dashboard.py` | Builds the page data from the database |
| `template.html` | The dashboard page (data is embedded at build time) |
| `__main__.py` | The `python3 -m tracker` commands |
| `data/` | **Gitignored.** The database, the built dashboard, your local config |

## Privacy

`tracker/data/` holds real people's names, headlines and comments. It is gitignored
on purpose: this repo is public. Don't move the database or a built dashboard
anywhere that gets committed.
