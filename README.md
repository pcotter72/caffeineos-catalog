# caffeineos-catalog

The software catalog for **CaffeineOSDT**, the download and update tool of
CaffeineOS (AmigaOS 3.x on PiStorm/Emu68).

CaffeineOSDT on the Amiga downloads one file,
[`catalog.txt`](catalog.txt)
(`https://raw.githubusercontent.com/pcotter72/caffeineos-catalog/main/catalog.txt`),
and compares each entry with the version of the installed program. It never
queries GitHub per program.

## How it works

- [`sources.txt`](sources.txt) lists the programs: where they come from (a GitHub
  repository's latest release, or a fixed URL for our own tools), which files they
  install and where, and which installed file carries the version.
- [`tools/build_catalog.py`](tools/build_catalog.py) downloads each release, reads
  the `$VER` string of the program inside the archive, and writes `catalog.txt`
  with version, date, git build, download URL, size and MD5.
- The [Update catalog](.github/workflows/update-catalog.yml) workflow runs it
  daily, on every change to `sources.txt`, and by hand ("Run workflow"). It commits
  `catalog.txt` only when something changed.

Versions are never typed by hand; they always come from the program itself.

## Adding a program

Add a `[Program:Name]` section to `sources.txt` (the format is described at the
top of the file) and push. The workflow builds the new entry; a program that
cannot be built is listed as a comment in `catalog.txt` and in the run log, and
the other entries are kept.

Supported archives: `zip`, `lha`, and `file` (a single program, no archive).

## Running it locally

```
python3 tools/build_catalog.py sources.txt catalog.txt
```

Optional: `GITHUB_TOKEN` in the environment raises the GitHub API rate limit.
`lha` archives need `lhasa` (or `lha`) on the PATH.
