#!/usr/bin/env python3
"""Build catalog.txt for CaffeineOSDT from sources.txt.

For every [Program:...] section in sources.txt the script finds the newest
release (GitHub API) or takes the fixed URL, downloads the archive, reads the
$VER string of the Check file inside it, computes size and MD5, and writes one
section to the catalog. The Amiga side (CaffeineOSDT) fetches only catalog.txt.

Usage:  build_catalog.py sources.txt catalog.txt
Env:    GITHUB_TOKEN (optional, raises the GitHub API rate limit)

Standard library only. Archives: zip (zipfile), lha (needs 'lha' or 'lhasa'
on PATH), file (a single binary, no archive).
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

USER_AGENT = 'caffeineos-catalog-builder'


# ---------------------------------------------------------------- sources.txt
def parse_sources(path):
    """INI-like: [Program:Name] sections, Key=Value lines, File= may repeat."""
    programs = []
    cur = None
    with open(path, encoding='ascii') as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line[0] in '#;':
                continue
            if line.startswith('[') and line.endswith(']'):
                kind, _, name = line[1:-1].partition(':')
                if kind != 'Program' or not name:
                    sys.exit(f'{path}:{lineno}: unknown section {line}')
                cur = {'Name': name, 'File': []}
                programs.append(cur)
                continue
            if cur is None or '=' not in line:
                sys.exit(f'{path}:{lineno}: expected Key=Value inside a [Program:...] section')
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if key == 'File':
                cur['File'].append(value)
            else:
                cur[key] = value
    for p in programs:
        for need in ('Archive', 'Check'):
            if need not in p:
                sys.exit(f"[Program:{p['Name']}]: {need}= missing")
        if ('GitHub' in p) == ('URL' in p):
            sys.exit(f"[Program:{p['Name']}]: give exactly one of GitHub= or URL=")
        if not p['File']:
            sys.exit(f"[Program:{p['Name']}]: at least one File= line needed")
    return programs


# --------------------------------------------------------------------- network
def http_get(url, accept=None):
    headers = {'User-Agent': USER_AGENT}
    if accept:
        headers['Accept'] = accept
    token = os.environ.get('GITHUB_TOKEN')
    if token and url.startswith('https://api.github.com/'):
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # network hiccup: retry twice
            if attempt == 2:
                raise
            print(f'  retry after error: {e}', file=sys.stderr)
            time.sleep(5)


def latest_github_asset(repo, pattern):
    """Newest non-prerelease release of repo and the asset matching pattern."""
    rel = json.loads(http_get(f'https://api.github.com/repos/{repo}/releases/latest',
                              'application/vnd.github+json'))
    assets = [a for a in rel.get('assets', []) if fnmatch.fnmatch(a['name'], pattern)]
    if len(assets) != 1:
        names = [a['name'] for a in rel.get('assets', [])]
        raise RuntimeError(f'{repo} {rel["tag_name"]}: {len(assets)} assets match {pattern!r} ({names})')
    return rel['tag_name'], assets[0]['browser_download_url']


# -------------------------------------------------------------------- archives
def extract(archive_bytes, kind, workdir):
    """Unpack into workdir; returns workdir."""
    if kind == 'zip':
        with tempfile.TemporaryFile() as t:
            t.write(archive_bytes)
            t.seek(0)
            with zipfile.ZipFile(t) as z:
                for info in z.infolist():
                    target = os.path.realpath(os.path.join(workdir, info.filename))
                    if not target.startswith(os.path.realpath(workdir) + os.sep) and \
                            target != os.path.realpath(workdir):
                        raise RuntimeError(f'zip entry escapes the work dir: {info.filename}')
                z.extractall(workdir)
    elif kind == 'lha':
        arc = os.path.join(workdir, '_archive.lha')
        with open(arc, 'wb') as f:
            f.write(archive_bytes)
        tool = shutil.which('lhasa') or shutil.which('lha')
        if not tool:
            raise RuntimeError('lha archive, but neither lhasa nor lha is on PATH')
        subprocess.run([tool, 'x', '_archive.lha'], cwd=workdir, check=True,
                       stdout=subprocess.DEVNULL)
        os.remove(arc)
    elif kind == 'file':
        pass  # handled by the caller: the download is the file itself
    else:
        raise RuntimeError(f'unknown Archive={kind}')
    return workdir


# ------------------------------------------------------------------ $VER parse
VER_TAG = b'$VER:'


def plausible(s):
    if len(s) < 3 or any(c < 0x20 or c > 0x7e for c in s) or b'%' in s:
        return False
    return re.search(rb'\d\.\d', s) is not None


def read_ver(data):
    """First plausible $VER string (same rules as CaffeineOSDT's read_ver_string)."""
    pos = data.find(VER_TAG)
    while pos >= 0:
        end = pos
        while end < len(data) and end - pos < 256 and data[end] not in (0, 10, 13):
            end += 1
        s = data[pos:end]
        while True:
            s = s.lstrip(b' \t')
            if s.startswith(VER_TAG):
                s = s[len(VER_TAG):]
                continue
            break
        if plausible(s):
            return s.decode('ascii')
        pos = data.find(VER_TAG, max(end, pos + 5))
    return None


def split_ver(ver):
    """'EmuControl 1.5 (30.12.2025) git:b615589' -> ('1.5', '30.12.2025', 'b615589')."""
    m = re.search(r'(?:^|\s)(\d+(?:\.\d+)+)(?=\s|\(|$)', ver)
    version = m.group(1) if m else ''
    d = re.search(r'\((\d{1,2}\.\d{1,2}\.\d{2,4})\)', ver)
    date = d.group(1) if d else ''
    g = re.search(r'git:([0-9a-fA-F]{7,40})', ver)
    return version, date, (g.group(1) if g else '')


# ----------------------------------------------------------------------- build
def build_entry(p, cache):
    if 'GitHub' in p:
        tag, url = latest_github_asset(p['GitHub'], p.get('Asset', '*'))
        source = f"github:{p['GitHub']}"
    else:
        tag, url = '', p['URL']
        source = 'url'
    if url not in cache:
        print(f'  download {url}', file=sys.stderr)
        cache[url] = http_get(url)
    blob = cache[url]

    with tempfile.TemporaryDirectory() as work:
        if p['Archive'] == 'file':
            check_bytes = blob
        else:
            extract(blob, p['Archive'], work)
            check_src = None
            for fl in p['File']:
                src, _, dest = (x.strip() for x in fl.partition('>'))
                if not os.path.exists(os.path.join(work, *src.split('/'))):
                    raise RuntimeError(f"[Program:{p['Name']}]: File {src} not in the archive")
                if dest.lower() == p['Check'].lower():
                    check_src = src
            if not check_src:
                raise RuntimeError(f"[Program:{p['Name']}]: no File= line installs to Check={p['Check']}")
            with open(os.path.join(work, *check_src.split('/')), 'rb') as f:
                check_bytes = f.read()

    ver = read_ver(check_bytes)
    if not ver:
        raise RuntimeError(f"[Program:{p['Name']}]: no $VER in the Check file")
    version, date, build = split_ver(ver)
    if not version:
        raise RuntimeError(f"[Program:{p['Name']}]: no version number in $VER '{ver}'")

    out = [f"[Program:{p['Name']}]"]
    for key in ('Category', 'Description'):
        if key in p:
            out.append(f'{key}={p[key]}')
    out.append(f'Version={version}')
    out.append(f'Date={date}')
    if build:
        out.append(f'Build={build}')
    out.append(f'VerString={ver}')
    if tag:
        out.append(f'Release={tag}')
    out.append(f'Source={source}')
    out.append(f'URL={url}')
    out.append(f"Archive={p['Archive']}")
    out.append(f'Size={len(blob)}')
    out.append(f'MD5={hashlib.md5(blob).hexdigest()}')
    for fl in p['File']:
        out.append(f'File={fl}')
    out.append(f"Check={p['Check']}")
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sources, target = sys.argv[1], sys.argv[2]
    programs = parse_sources(sources)
    cache = {}
    body = []
    failed = 0
    for p in programs:
        print(f"{p['Name']}:", file=sys.stderr)
        try:
            body += build_entry(p, cache) + ['']
        except Exception as e:
            failed += 1
            print(f'  ERROR {e}', file=sys.stderr)
            body += [f"# [Program:{p['Name']}] skipped: {e}", '']

    text_body = '\n'.join(body)
    old_body = None
    if os.path.exists(target):
        with open(target, encoding='ascii') as f:
            old = f.read()
        old_body = old.split('\n\n', 1)[1] if '\n\n' in old else ''
    if old_body == text_body:
        print('catalog unchanged', file=sys.stderr)
    else:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        head = ['# CaffeineOS software catalog for CaffeineOSDT.',
                '# Generated by tools/build_catalog.py from sources.txt - do not edit by hand.',
                'CatalogVersion=1',
                f'Generated={stamp}']
        text = '\n'.join(head) + '\n\n' + text_body
        text.encode('ascii')  # the Amiga side expects plain ASCII
        with open(target, 'w', encoding='ascii', newline='\n') as f:
            f.write(text)
        print(f'catalog written: {len(programs) - failed} program(s), {failed} skipped', file=sys.stderr)
    # A skipped program keeps the run green but is visible in the catalog and the log;
    # fail the run only when nothing could be built.
    if programs and failed == len(programs):
        sys.exit(1)


if __name__ == '__main__':
    main()
