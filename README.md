# opoznienia-archive

This repository stores archived source files from `https://pkp-archive.2137.workers.dev/`.

It also generates a root `index.json` file with the same top-level shape as the upstream endpoint:

```json
{
	"error": true,
	"files": {
		"2026-3-5": ["https://raw.githubusercontent.com/..."],
		"2026-3-4": ["https://raw.githubusercontent.com/..."]
	}
}
```

The values in `files` point to raw GitHub URLs for the archived files in this repository.

## Layout

Downloaded files are stored under `archive/YYYY/MM/DD/`.

Each day directory contains:

- `source-001.json`, `source-002.json`, ... for the raw archived files
- `manifest.json` with the original source URLs and file metadata

## Automation

GitHub Actions runs the archiver every day at `04:00` Europe/Warsaw time.

GitHub Actions schedules use UTC, so the workflow is triggered at both `02:00` and `03:00` UTC and then gated at runtime to run only when the local Warsaw hour is `04`. That keeps the schedule correct across DST changes.

The workflow downloads only sources that are not already represented in the per-day manifest and commits the new files back to the repository.

## Local run

```bash
python scripts/archive_delays.py
```

Useful test flags:

```bash
python scripts/archive_delays.py --limit-dates 2 --limit-files-per-date 1
python scripts/archive_delays.py --dry-run --limit-dates 5
```