#!/usr/bin/env python3
"""Check whether the first DNG root is subsumed by the second. See README.md."""
from __future__ import annotations

import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import database_backend as sqlite3
import subprocess
import sys
import time

import numpy as np
import tifffile

VERSION = 4
CHUNK = 8 * 1024 * 1024
# Tags affecting the interpretation of encoded samples, not rendering settings.
ENCODING = (256, 257, 258, 259, 262, 266, 277, 278, 284, 317, 322,
            323, 338, 339, 347, 512, 515, 517, 518, 519, 520, 521,
            529, 530, 531, 532, 50871, 50974, 52543, 52544)
LAYOUT = {'StripOffsets', 'StripByteCounts', 'TileOffsets', 'TileByteCounts',
          'PreviewImageStart', 'PreviewImageLength', 'JpgFromRawStart',
          'JpgFromRawLength', 'ThumbnailOffset', 'ThumbnailLength',
          'FreeOffsets', 'FreeByteCounts'}
IMAGE_TAGS = ('PreviewImage', 'JpgFromRaw', 'ThumbnailImage', 'OtherImage',
              'RawImage', 'OriginalRawFileData')


def dumps(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def stamp(path):
    s = os.stat(path)
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def inventory(root):
    found = collections.defaultdict(list)
    def fail(error):
        raise error  # Never silently omit an unreadable directory.
    for directory, dirs, files in os.walk(root, onerror=fail):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith('.dng') and not name.startswith('._'):
                found[name].append(os.path.join(directory, name))
    return found


def read_pairs(path):
    pairs = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) != 2 or not all(fields):
            raise ValueError(f'{path}:{number}: expected LEFT<TAB>RIGHT')
        pairs.append(fields)
    return pairs


def resolve(name, root, found, known):
    """A listed pair member: a path, absolute or relative to its root, or a basename unique there."""
    joined = os.path.join(root, name)
    path = os.path.join(os.path.realpath(os.path.dirname(joined)), os.path.basename(joined))
    if path in known:
        return path
    candidates = found.get(name, []) if os.sep not in name else []
    if len(candidates) == 1:
        return candidates[0]
    problem = f'{len(candidates)} DNGs have this name' if candidates else 'not a DNG'
    raise ValueError(f'{name}: {problem} under {root}')


def explicit_pairs(listed, roots, left, right):
    """Map each listed left path to its listed right paths, in the order given."""
    known = [{p for paths in found.values() for p in paths} for found in (left, right)]
    explicit = {}
    for names in listed:
        source, target = (resolve(*args) for args in zip(names, roots, (left, right), known))
        if target not in explicit.setdefault(source, []):
            explicit[source].append(target)
    return explicit


def match(left, right, roots=None, explicit=None):
    # Listed pairs replace name matching for their left files, e.g. renamed copies.
    explicit = explicit or {}
    pairs, duplicates = [], []
    for name in sorted(left):
        candidates = right.get(name, [])
        named = [source for source in left[name] if source not in explicit]
        if named and (len(left[name]) > 1 or len(candidates) > 1):
            duplicates.append({'name': name, 'left': left[name], 'right': candidates})
        for source in left[name]:
            if source in explicit:
                pairs.extend((source, target) for target in explicit[source])
                continue
            ordered = sorted(candidates, key=lambda p: (
                roots is not None and os.path.relpath(source, roots[0]) != os.path.relpath(p, roots[1]), p))
            pairs.extend((source, target) for target in ordered)
    return pairs, duplicates


def pages(pageset):
    for page in pageset:
        yield page
        if page.pages:
            yield from pages(page.pages)


def page_groups(tiff):
    raw, auxiliary = [], []
    for page in pages(tiff.pages):
        subtype = int(page.subfiletype)
        # CFA / LinearRaw main images. Some valid DNGs use RGB/grayscale.
        primary = subtype == 0 and int(page.photometric) in (0, 1, 2, 32803, 34892)
        (raw if primary else auxiliary).append(page)
    return raw, auxiliary


def signature(page):
    return (page.parent.byteorder, tuple(
        (code, repr(page.tags[code].value)) for code in ENCODING if code in page.tags))


def payload_digest(handle, page, size):
    offsets, counts = page.dataoffsets, page.databytecounts
    if not offsets or len(offsets) != len(counts):
        raise ValueError('Image has no complete strip/tile directory')
    digest = hashlib.sha256()
    total = 0
    for offset, count in zip(offsets, counts):
        if offset <= 0 or count <= 0 or offset + count > size:
            raise ValueError(f'Invalid image block: offset={offset}, length={count}')
        handle.seek(offset)
        digest.update(int(count).to_bytes(8, 'little'))
        remaining = count
        while remaining:
            data = handle.read(min(CHUNK, remaining))
            if not data:
                raise EOFError('Truncated image block')
            digest.update(data)
            remaining -= len(data)
        total += count
    return digest.hexdigest(), total


def compare_page(a, b, fa, fb, size_a, size_b):
    result = {'left_shape': list(a.shape), 'right_shape': list(b.shape)}
    da, na = payload_digest(fa, a, size_a)
    db, nb = payload_digest(fb, b, size_b)
    result.update(left_sha256=da, right_sha256=db, bytes_read=na + nb)
    if signature(a) == signature(b) and da == db:
        result.update(status='equal', method='encoded_blocks')
        return result
    try:
        aa, bb = a.asarray(maxworkers=1), b.asarray(maxworkers=1)
        equal = aa.shape == bb.shape and np.array_equal(aa, bb, equal_nan=True)
        result.update(status='equal' if equal else 'different', method='decoded_samples')
    except Exception as exc:
        # Different compressed bytes are NOT proof of different pixels.
        result.update(status='unverified', method='decode_failed', error=str(exc))
    return result


def group_status(results):
    statuses = {r['status'] for r in results}
    if not results or 'unverified' in statuses:
        return 'unverified'
    return 'different' if 'different' in statuses else 'equal'


def compare_pixels(pair):
    left, right = pair
    result = {'left': left, 'right': right}
    try:
        before = [stamp(left), stamp(right)]
        with tifffile.TiffFile(left) as ta, tifffile.TiffFile(right) as tb:
            if 50706 not in ta.pages[0].tags or 50706 not in tb.pages[0].tags:
                raise ValueError('Missing DNGVersion tag')
            ga, gb = page_groups(ta), page_groups(tb)
            for name, pa, pb in zip(('raw', 'auxiliary'), ga, gb):
                if len(pa) != len(pb):
                    result[name] = {'status': 'unverified', 'error': 'Image count differs',
                                    'left_count': len(pa), 'right_count': len(pb)}
                    continue
                images = [compare_page(a, b, ta.filehandle, tb.filehandle,
                                       before[0][2], before[1][2]) for a, b in zip(pa, pb)]
                status = group_status(images) if images or name == 'raw' else 'equal'
                result[name] = {'status': status, 'images': images}
        if before != [stamp(left), stamp(right)]:
            raise ValueError('File changed during pixel comparison')
        result['stamps'] = before
    except Exception as exc:
        result['raw'] = {'status': 'unverified', 'error': str(exc)}
        result['error'] = str(exc)
    return result


def flatten(value, prefix=''):
    out = {}
    if isinstance(value, dict):
        for key, child in value.items():
            out.update(flatten(child, f'{prefix}/{key}' if prefix else key))
    else:
        out[prefix] = value
    return out


def compact(value):
    encoded = dumps(value)
    if len(encoded) > 1024:
        return {'sha256': hashlib.sha256(encoded.encode()).hexdigest(),
                'json_length': len(encoded), 'prefix': encoded[:120]}
    return value


def normalize_tags(entry):
    # ExifTool's CopyN numbering is global: adding XMP can renumber unchanged
    # EXIF tags. Preserve real duplicates within each group without that noise.
    groups = collections.defaultdict(list)
    for key, value in entry.items():
        key = re.sub(r':Copy\d+(?=:)', '', key)
        groups[key].append(value)
    return {key: values[0] if len(values) == 1 else
            {'duplicate_values': sorted(values, key=dumps)} for key, values in groups.items()}


def metadata(paths):
    # One process per batch, not per file. -b compares binary metadata contents,
    # rather than ExifTool's usual "Binary data N bytes" placeholders.
    command = ['exiftool', '-j', '-G1:4', '-n', '-struct', '-b', '-u',
               '-api', 'LargeFileSupport=1', '-all', '-DNGPrivateData', '-MakerNotes']
    for tag in IMAGE_TAGS:
        command += ['-x', tag]
    proc = subprocess.run(command + paths, capture_output=True, text=True)
    try:
        entries = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(f'ExifTool failed: {proc.stderr[:1000]}') from exc
    result = {}
    for entry in entries:
        path = entry.pop('SourceFile')
        errors = [str(v) for k, v in entry.items() if k.split(':')[-1] == 'Error']
        if errors:
            result[path] = {'error': '; '.join(errors)}
        else:
            result[path] = {'tags': flatten(normalize_tags(entry)), 'warnings': [str(v) for k, v in entry.items()
                              if k.split(':')[-1] == 'Warning']}
    return result


def compare_metadata(a, b):
    differences = {'embedded': [], 'filesystem': [], 'layout': []}
    for key in sorted(a.keys() | b.keys()):
        if key.startswith(('ExifTool:', 'Composite:')):
            continue
        if key in a and key in b and a[key] == b[key]:
            continue
        tag = key.split(':')[-1]
        if tag in {'Directory', 'FileName', 'FileAccessDate'} and key.startswith('System:'):
            continue
        category = ('filesystem' if key.startswith('System:') else
                    'layout' if tag in LAYOUT else 'embedded')
        change = {'tag': key, 'kind': 'added' if key not in a else
                  'removed' if key not in b else 'changed'}
        if key in a:
            change['left'] = compact(a[key])
        if key in b:
            change['right'] = compact(b[key])
        differences[category].append(change)
    return differences


class Summary:
    def __init__(self):
        self.counts = collections.Counter()
        self.tags = {}
        self.patterns = collections.Counter()
        self.bytes_read = 0
        self.coverage = {}

    def add(self, result):
        covered = self.coverage.setdefault(result['left'], {'raw': False, 'all_images': False,
                                                           'metadata_preserved': False})
        raw_equal = result['raw']['status'] == 'equal'
        covered['raw'] |= raw_equal
        covered['all_images'] |= raw_equal and result.get('auxiliary', {}).get('status') == 'equal'
        covered['metadata_preserved'] |= (raw_equal and result.get('metadata_status') == 'ok'
            and not result.get('metadata_warnings') and not any(
                c['kind'] != 'added' for c in result.get('differences', {}).get('embedded', [])))
        self.counts['pairs_reported'] += 1
        self.counts['raw_' + result['raw']['status']] += 1
        self.counts['auxiliary_' + result.get('auxiliary', {}).get('status', 'unverified')] += 1
        self.counts['metadata_' + result.get('metadata_status', 'error')] += 1
        if result.get('metadata_warnings'):
            self.counts['metadata_with_warnings'] += 1
        for group in ('raw', 'auxiliary'):
            self.bytes_read += sum(p.get('bytes_read', 0) for p in result.get(group, {}).get('images', []))
        embedded = result.get('differences', {}).get('embedded', [])
        if embedded:
            self.counts['embedded_metadata_different'] += 1
        pattern = tuple(f"{c['kind']}:{c['tag']}" for c in embedded)
        if result.get('metadata_status') == 'ok':
            self.patterns[pattern] += 1
        for category, changes in result.get('differences', {}).items():
            for change in changes:
                key = f"{category}/{change['kind']}:{change['tag']}"
                record = self.tags.setdefault(key, {'count': 0, 'examples': []})
                record['count'] += 1
                example = {**change, 'file': result['left']}
                if len(record['examples']) < 3:
                    record['examples'].append(example)

    def write(self, path, context, complete, elapsed):
        coverage_counts = {key: sum(c[key] for c in self.coverage.values())
                           for key in ('raw', 'all_images', 'metadata_preserved')}
        subsumption = {key: count == context['left_files'] for key, count in coverage_counts.items()}
        report = {**context, 'complete': complete, 'elapsed_seconds': round(elapsed, 2),
                  'left_files_covered': coverage_counts, 'left_subsumed_by_right': subsumption,
                  'counts': dict(self.counts), 'verified_payload_bytes': self.bytes_read,
                  'metadata_patterns': [{'count': count, 'changes': list(pattern)}
                                        for pattern, count in self.patterns.most_common()],
                  'metadata_tags': dict(sorted(self.tags.items(),
                                               key=lambda item: -item[1]['count']))}
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(report, indent=2) + '\n')
        temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left', type=Path)
    parser.add_argument('right', type=Path)
    parser.add_argument('--output', type=Path, default=Path('dng-report'))
    parser.add_argument('--workers', type=int, default=2, help='Concurrent pairs; default 2 for hard drives')
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--limit', type=int, help='Compare only N pairs (reported as a partial scan)')
    parser.add_argument('--sample', type=int, help='Compare N evenly spaced pairs across the roots')
    parser.add_argument('--fresh', action='store_true', help='Ignore saved comparisons')
    parser.add_argument('--pair', nargs=2, action='append', default=[], metavar=('LEFT', 'RIGHT'),
                        help='Compare LEFT with RIGHT instead of matching LEFT by name; repeatable')
    parser.add_argument('--pairs', type=Path, help='File of LEFT<TAB>RIGHT lines, as for --pair')
    args = parser.parse_args()
    if args.workers < 1 or args.batch_size < 1 or any(v is not None and v < 1 for v in (args.limit, args.sample)):
        parser.error('workers, batch-size, limit and sample must be positive')
    if args.limit and args.sample:
        parser.error('Choose either --limit or --sample')
    if not shutil.which('exiftool'):
        parser.error('ExifTool is required: brew install exiftool')
    roots = [str(p.resolve()) for p in (args.left, args.right)]
    if not all(os.path.isdir(p) for p in roots):
        parser.error('Both roots must be readable directories')
    args.output.mkdir(parents=True, exist_ok=True)
    # Prevent concurrent writers to one cache/report directory.
    import fcntl
    lock = open(args.output / '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('Another comparison is already using this output directory')
    started = time.monotonic()
    print('Indexing both roots...', flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(inventory, roots))
    try:
        explicit = explicit_pairs(args.pair + (read_pairs(args.pairs) if args.pairs else []),
                                  roots, left, right)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    pairs, duplicates = match(left, right, roots, explicit)
    paired = {source for source, _ in pairs}
    left_only = {name: unpaired for name, paths in sorted(left.items())
                 if (unpaired := [p for p in paths if p not in paired])}
    total = len(pairs)
    context = {'version': VERSION, 'roots': roots, 'matched_pairs': total,
               'explicit_pairs': sum(map(len, explicit.values())),
               'left_files': sum(map(len, left.values())),
               'right_files': sum(map(len, right.values())),
               'duplicate_names': len(duplicates),
               'left_only_names': len(left_only),
               'direction': 'left must be contained in right; extra right files are ignored'}
    with open(args.output / 'unmatched.json', 'w') as out:
        json.dump({'duplicate_candidates': duplicates,
                   'left_only': left_only}, out)
    if args.sample and len(pairs) > args.sample:
        indices = np.linspace(0, len(pairs) - 1, args.sample, dtype=int)
        pairs = [pairs[i] for i in indices]
    elif args.limit:
        pairs = pairs[:args.limit]
    context['selected_pairs'] = len(pairs)
    print(f'{total:,} candidate pairs; processing {len(pairs):,}; duplicate names: {len(duplicates)}', flush=True)
    db = sqlite3.connect(args.output / 'cache.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, stamps TEXT, result TEXT)')
    summary = Summary()
    reused = 0
    try:
        with open(args.output / 'pairs.jsonl', 'w') as out, ThreadPoolExecutor(max_workers=args.workers) as pool:
            for start in range(0, len(pairs), args.batch_size):
                batch = pairs[start:start + args.batch_size]
                results, pending, initial_stamps = {}, [], {}
                for pair in batch:
                    key = dumps([VERSION, *pair])
                    row = None if args.fresh else db.execute('SELECT stamps,result FROM results WHERE key=?', (key,)).fetchone()
                    try:
                        current = dumps([stamp(p) for p in pair])
                    except OSError:
                        current = None
                    if row and row[0] == current:
                        results[pair] = json.loads(row[1])
                        reused += 1
                    else:
                        pending.append(pair)
                        initial_stamps[pair] = current
                # Pixel work and batch metadata extraction overlap.
                futures = {pair: pool.submit(compare_pixels, pair) for pair in pending}
                try:
                    meta = metadata([p for pair in pending for p in pair]) if pending else {}
                    metadata_error = None
                except Exception as exc:
                    meta, metadata_error = {}, str(exc)
                for pair, future in futures.items():
                    result = future.result()
                    a, b = [meta.get(p, {'error': metadata_error or 'ExifTool returned no record'}) for p in pair]
                    if 'error' in a or 'error' in b:
                        result.update(metadata_status='error', metadata_error=[a.get('error'), b.get('error')])
                    else:
                        result.update(metadata_status='ok', differences=compare_metadata(a['tags'], b['tags']),
                                      metadata_warnings=a['warnings'] + b['warnings'])
                    try:
                        if (result.get('stamps') != [stamp(p) for p in pair]
                                or dumps(result.get('stamps')) != initial_stamps[pair]):
                            raise ValueError('File changed or was unreadable during comparison')
                    except (ValueError, OSError) as exc:
                        result['error'] = str(exc)
                        result['raw']['status'] = 'unverified'
                        result['metadata_status'] = 'error'
                    results[pair] = result
                    if (not result.get('error') and result['metadata_status'] == 'ok'
                            and result['raw']['status'] != 'unverified'
                            and result['auxiliary']['status'] != 'unverified'):
                        db.execute('INSERT OR REPLACE INTO results VALUES (?,?,?)',
                                   (dumps([VERSION, *pair]), dumps(result['stamps']), dumps(result)))
                for pair in batch:
                    result = results[pair]
                    out.write(dumps(result) + '\n')
                    summary.add(result)
                out.flush()
                db.commit()
                elapsed = time.monotonic() - started
                context['cached_pairs_reused'] = reused
                summary.write(args.output / 'summary.json', context, False, elapsed)
                done = summary.counts['pairs_reported']
                print(f"{done:,}/{len(pairs):,} | raw equal={summary.counts['raw_equal']:,} "
                      f"different={summary.counts['raw_different']:,} unverified={summary.counts['raw_unverified']:,} "
                      f"| {elapsed:.1f}s | cached={reused:,}", flush=True)
    except KeyboardInterrupt:
        print('Interrupted; rerun the same command to resume from the cache.', file=sys.stderr)
        return 130
    finally:
        db.close()
    complete = len(pairs) == total
    summary.write(args.output / 'summary.json', context, complete, time.monotonic() - started)
    uncovered = [p for paths in left.values() for p in paths
                 if not summary.coverage.get(p, {}).get('raw')]
    (args.output / 'not-confirmed.json').write_text(json.dumps(uncovered, indent=2) + '\n')
    print(f'Raw pixels preserved for {context["left_files"] - len(uncovered):,}/{context["left_files"]:,} left files.', flush=True)
    return 0 if not uncovered else 1


if __name__ == '__main__':
    sys.exit(main())
