#!/usr/bin/env python3
"""Build catalog.txt for CaffeineOSDT from sources.txt.

For every [Program:...] section in sources.txt the script finds the newest
release (GitHub API, Aminet readme, or a fixed URL), downloads the archive,
reads the $VER string of the Check file inside it, computes size and MD5, and
writes one section to the catalog. The Amiga side (CaffeineOSDT) fetches only
catalog.txt.

An archive is downloaded only when its upstream marker changed (GitHub tag,
Aminet readme version + date, or the fixed URL) or the program's section in
sources.txt was edited; otherwise the previous catalog entry is kept.

Usage:  build_catalog.py sources.txt catalog.txt
Env:    GITHUB_TOKEN (optional, raises the GitHub API rate limit)

Standard library only. Archives: zip (zipfile), lha (needs 'lhasa' or 'lha'
on PATH), file (a single binary, no archive).
"""
import fnmatch
import glob
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

USER_AGENT = 'caffeineos-catalog-builder (+https://github.com/pcotter72/caffeineos-catalog)'
AMINET = 'https://aminet.net'
RAW_BASE = 'https://raw.githubusercontent.com/pcotter72/caffeineos-catalog/main/'
READMES_DIR = 'readmes'     # set next to catalog.txt in main()
# Part of every entry's SourceHash: raising it rebuilds all entries once
# (2 = Readme= added, 05.11.2026).
BUILDER_REV = '2'


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
                cur = {'Name': name, 'File': [], '_lines': []}
                programs.append(cur)
                continue
            if cur is None or '=' not in line:
                sys.exit(f'{path}:{lineno}: expected Key=Value inside a [Program:...] section')
            cur['_lines'].append(line)
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
        if sum(k in p for k in ('GitHub', 'URL', 'Aminet')) != 1:
            sys.exit(f"[Program:{p['Name']}]: give exactly one of GitHub=, Aminet= or URL=")
        if not p['File']:
            sys.exit(f"[Program:{p['Name']}]: at least one File= line needed")
        p['_hash'] = hashlib.md5(('\n'.join(p['_lines']) + '\nrev=' + BUILDER_REV)
                                 .encode('ascii')).hexdigest()[:12]
    return programs


def parse_catalog(path):
    """Previous catalog.txt -> {name: [lines]} (only real entries)."""
    entries = {}
    if not os.path.exists(path):
        return entries
    cur = None
    with open(path, encoding='ascii') as f:
        for raw in f:
            line = raw.rstrip('\n')
            if line.startswith('[Program:') and line.endswith(']'):
                cur = line[9:-1]
                entries[cur] = [line]
            elif cur is not None:
                if line == '':
                    cur = None
                else:
                    entries[cur].append(line)
    return entries


def entry_value(lines, key):
    for line in lines:
        if line.startswith(key + '='):
            return line[len(key) + 1:]
    return None


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
            with urllib.request.urlopen(req, timeout=120) as r:
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


def aminet_package(path):
    """'util/dtype/JFIFdt44' -> (marker from the readme, archive URL).

    The marker is the readme's Version: field plus an MD5 of the readme, so a
    re-upload with a changed readme is noticed even when Version: stays.
    """
    readme = http_get(f'{AMINET}/{path}.readme')
    text = readme.decode('latin-1')
    m = re.search(r'^Version:\s*(\S.*?)\s*$', text, re.M)
    version = m.group(1) if m else '?'
    marker = f'{version}/{hashlib.md5(readme).hexdigest()[:8]}'
    return marker, f'{AMINET}/{path}.lha'


# -------------------------------------------------------------------- archives
def extract(archive_bytes, kind, workdir):
    """Unpack into workdir."""
    real = os.path.realpath(workdir)
    if kind == 'zip':
        with tempfile.TemporaryFile() as t:
            t.write(archive_bytes)
            t.seek(0)
            with zipfile.ZipFile(t) as z:
                for info in z.infolist():
                    target = os.path.realpath(os.path.join(workdir, info.filename))
                    if not (target == real or target.startswith(real + os.sep)):
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
        for root, dirs, files in os.walk(workdir):
            for n in dirs + files:
                target = os.path.realpath(os.path.join(root, n))
                if not target.startswith(real + os.sep):
                    raise RuntimeError(f'lha entry escapes the work dir: {n}')
    elif kind != 'file':
        raise RuntimeError(f'unknown Archive={kind}')


def resolve(work, pattern):
    """Archive path with * and ? wildcards -> the one matching relative path."""
    hits = [h for h in glob.glob(os.path.join(work, *pattern.split('/'))) if os.path.isfile(h)]
    if len(hits) != 1:
        raise RuntimeError(f'{pattern}: {len(hits)} files match in the archive')
    return os.path.relpath(hits[0], work).replace(os.sep, '/')


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
def upstream(p):
    """(source label, marker, download URL) without downloading the archive."""
    if 'GitHub' in p:
        tag, url = latest_github_asset(p['GitHub'], p.get('Asset', '*'))
        return f"github:{p['GitHub']}", tag, url
    if 'Aminet' in p:
        marker, url = aminet_package(p['Aminet'])
        return f"aminet:{p['Aminet']}", marker, url
    return 'url', p['URL'], p['URL']


def build_entry(p, source, marker, url, cache):
    if url not in cache:
        print(f'  download {url}', file=sys.stderr)
        cache[url] = http_get(url)
    blob = cache[url]

    files = []
    with tempfile.TemporaryDirectory() as work:
        if p['Archive'] == 'file':
            if len(p['File']) != 1:
                raise RuntimeError('Archive=file takes exactly one File= line')
            check_bytes = blob
            files = p['File']
        else:
            extract(blob, p['Archive'], work)
            check_src = None
            for fl in p['File']:
                src, _, dest = (x.strip() for x in fl.partition('>'))
                real_src = resolve(work, src)
                files.append(f'{real_src} > {dest}')
                if dest.lower() == p['Check'].lower():
                    check_src = real_src
            if not check_src:
                raise RuntimeError(f"no File= line installs to Check={p['Check']}")
            with open(os.path.join(work, *check_src.split('/')), 'rb') as f:
                check_bytes = f.read()

        # Readme shown by CaffeineOSDT's "Show README": an explicit Readme= URL,
        # a file from the archive (ReadmeFile=, published under readmes/), or
        # for Aminet packages the package's own .readme.
        readme_url = p.get('Readme')
        if not readme_url and 'ReadmeFile' in p and p['Archive'] != 'file':
            rsrc = resolve(work, p['ReadmeFile'])
            rname = re.sub(r'[^A-Za-z0-9._-]', '_', rsrc.rsplit('/', 1)[-1])
            os.makedirs(READMES_DIR, exist_ok=True)
            shutil.copyfile(os.path.join(work, *rsrc.split('/')), os.path.join(READMES_DIR, rname))
            readme_url = RAW_BASE + 'readmes/' + rname
        if not readme_url and 'Aminet' in p:
            readme_url = f"{AMINET}/{p['Aminet']}.readme"

    ver = read_ver(check_bytes)
    if not ver:
        raise RuntimeError('no $VER in the Check file')
    version, date, build = split_ver(ver)
    if not version:
        raise RuntimeError(f"no version number in $VER '{ver}'")

    out = [f"[Program:{p['Name']}]"]
    for key in ('Category', 'Description'):
        if key in p:
            out.append(f'{key}={p[key]}')
    out.append(f'Version={version}')
    out.append(f'Date={date}')
    if build:
        out.append(f'Build={build}')
    out.append(f'VerString={ver}')
    out.append(f'Source={source}')
    out.append(f'Upstream={marker}')
    out.append(f'URL={url}')
    out.append(f"Archive={p['Archive']}")
    out.append(f'Size={len(blob)}')
    out.append(f'MD5={hashlib.md5(blob).hexdigest()}')
    for fl in files:
        out.append(f'File={fl}')
    out.append(f"Check={p['Check']}")
    if readme_url:
        out.append(f'Readme={readme_url}')
    if p.get('AllowScript', '').lower() == 'yes':
        out.append('AllowScript=yes')
    out.append(f"SourceHash={p['_hash']}")
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sources, target = sys.argv[1], sys.argv[2]
    global READMES_DIR
    READMES_DIR = os.path.join(os.path.dirname(os.path.abspath(target)), 'readmes')
    programs = parse_sources(sources)
    previous = parse_catalog(target)
    cache = {}
    body = []
    failed = 0
    for p in programs:
        print(f"{p['Name']}:", file=sys.stderr)
        try:
            source, marker, url = upstream(p)
            old = previous.get(p['Name'])
            if old and entry_value(old, 'Upstream') == marker and \
                    entry_value(old, 'SourceHash') == p['_hash'] and entry_value(old, 'URL') == url:
                print(f'  unchanged ({marker}), previous entry kept', file=sys.stderr)
                body += old + ['']
                continue
            body += build_entry(p, source, marker, url, cache) + ['']
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
