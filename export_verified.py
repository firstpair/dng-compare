#!/usr/bin/env python3
"""Export confirmed Python v4 raw matches to the Rust encoded-path TSV format."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
from preserve_apo import validate_output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cache',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--left-root',type=Path,required=True)
    parser.add_argument('--right-root',type=Path,required=True)
    args=parser.parse_args()
    left_root=args.left_root.resolve(strict=True);right_root=args.right_root.resolve(strict=True)
    target=args.output.absolute()
    validate_output(left_root,right_root,target.parent)
    if target.exists():raise FileExistsError('Export never overwrites an existing output: '+str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(target.name+'.partial')
    count=0;owned=False
    db=sqlite3.connect(args.cache.resolve(strict=True).as_uri()+'?mode=ro',uri=True)
    try:
        with temp.open('x') as out:
            owned=True
            for key,result_text in db.execute('SELECT key,result FROM results'):
                try:
                    version,left,right=json.loads(key);result=json.loads(result_text);stamps=result['stamps']
                    if version!=4 or result['raw']['status']!='equal':continue
                    if not Path(left).resolve().is_relative_to(left_root) or not Path(right).resolve().is_relative_to(right_root):continue
                    if result.get('error') or len(stamps)!=2 or any(len(stamp)!=5 for stamp in stamps):continue
                    fields=[os.fsencode(left).hex(),os.fsencode(right).hex()]
                    fields.extend(','.join(map(str,stamp)) for stamp in stamps)
                    out.write('\t'.join(fields)+'\n');count+=1
                except (KeyError,ValueError,TypeError):continue
            out.flush();os.fsync(out.fileno())
        os.link(temp,target)
    finally:
        db.close()
        if owned and temp.exists():temp.unlink()
    print(f'Exported {count} confirmed historical raw matches; Rust must recheck current identities: {target}')


if __name__=='__main__':main()
