"""Overlap verified-byte production with metadata batches using one inventory.

Native byte and metadata backends are selected by the existing environment.
Each SQLite database has exactly one owner; either stage retains its checkpoint.
"""
import os
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor

import preserve_apo as preservation
import portable_metadata as metadata


def inventory(source, reference, pairs):
    directories, entries = [], []
    for root, dirs, names in os.walk(source, onerror=lambda e: (_ for _ in ()).throw(e)):
        dirs.sort(); names.sort()
        directory = pathlib.Path(root)
        directories.append(dict(path=str(directory.relative_to(source)), attributes=preservation.attrs(directory)))
        for name in names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Unsupported source entry: ' + str(path))
            rel = str(path.relative_to(source))
            base = pathlib.Path(pairs.get(str(path), str(reference / rel)))
            if not base.is_file():base = None
            entries.append(dict(relative_path=rel, apo=str(path), bumer=str(base) if base else None,
                                apo_stamp=metadata.stamp(path), bumer_stamp=metadata.stamp(base) if base else None,
                                apo_filesystem=metadata.properties(path)))
    return directories, entries


def run(source, reference, archive, project, pairs, rate, workers, batch_size=16):
    prepared = inventory(source, reference, pairs)
    ready, condition = set(), threading.Condition()
    finished = False
    cancelled = False

    def verified(rel):
        with condition:
            ready.add(rel)
            condition.notify_all()

    def wait_verified(rels):
        with condition:
            if cancelled:raise ValueError('Unified preservation interrupted')
            while not all(rel in ready for rel in rels):
                if cancelled:raise ValueError('Unified preservation interrupted')
                if finished:
                    raise ValueError('Byte verification did not succeed for metadata batch: ' +
                                     ', '.join(rel for rel in rels if rel not in ready))
                condition.wait()

    # Metadata owns its own SQLite connection in this thread. The preservation
    # coordinator and its bounded native worker pool run in the calling thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='metadata-stage') as stages:
        result = stages.submit(metadata.snapshot, source, reference, archive, project, batch_size,
                               prepared_inventory=prepared[1], wait_verified=wait_verified)
        try:
            complete = preservation.scan(source, reference, archive, project, rate,
                                         delta_mode=True, workers=workers,
                                         prepared_inventory=prepared, on_verified=verified)
        except BaseException:
            with condition:
                cancelled = True
            raise
        finally:
            with condition:
                finished = True
                condition.notify_all()
        result.result()
    if not complete:raise ValueError('Unified byte preservation incomplete: ' + str(archive))
    return True
