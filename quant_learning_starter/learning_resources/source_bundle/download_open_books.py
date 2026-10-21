#!/usr/bin/env python3
"""Download only the three publicly offered book PDFs in this bundle.

Python 3.10+, standard library only. No paywall, login, or confirmation bypass.
An HTML response is an error, never an ebook. Failed partial files are removed.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def download_pdf(url: str, target: Path, timeout: float, max_bytes: int) -> dict:
    """Stream a PDF with a size bound and basic signature/end-marker checks.

    These are transport checks, not a complete PDF parser or a security scan.
    Existing files are not overwritten. Open the resulting book to verify it.
    """
    if target.exists():
        if target.stat().st_size >= 1024:
            with target.open('rb') as f:
                start = f.read(8)
                f.seek(max(0, target.stat().st_size - 2048))
                tail = f.read()
            if start.startswith(b'%PDF-') and b'%%EOF' in tail:
                return {'status': 'existing_pdf_skipped', 'path': str(target), 'bytes': target.stat().st_size}
        raise ValueError(f'Existing file is not a checked PDF; not overwriting: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + '.part')
    if partial.exists():
        raise FileExistsError(f'Partial file already exists; inspect or remove it first: {partial}')
    total = 0
    created_partial = False
    try:
        request = Request(url, headers={'User-Agent': 'QuantLearningOfficialPDFDownloader/1.0'})
        with urlopen(request, timeout=timeout) as response:
            first = response.read(8192)
            if not first.startswith(b'%PDF-'):
                raise ValueError('Source returned a webpage or non-PDF. Open the official book page in a browser; login/confirmation is not bypassed.')
            if len(first) > max_bytes:
                raise ValueError('PDF exceeds the configured size limit.')
            with partial.open('xb') as f:
                created_partial = True
                f.write(first)
                total = len(first)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError('PDF exceeds the configured size limit.')
                    f.write(chunk)
        if total < 1024:
            raise ValueError('PDF response is unexpectedly small.')
        with partial.open('rb') as f:
            f.seek(max(0, total - 2048))
            if b'%%EOF' not in f.read():
                raise ValueError('Missing PDF end marker; file may be incomplete.')
        # No existing target is overwritten during normal single-process use.
        if target.exists():
            raise FileExistsError(f'Target appeared during download; refusing to overwrite: {target}')
        partial.rename(target)
        return {'status': 'downloaded', 'path': str(target), 'bytes': total}
    except Exception:
        if created_partial:
            partial.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'books', help='Directory on your existing cloud or local disk')
    parser.add_argument('--only', choices=('probability', 'islp', 'convex'), action='append', help='Select a book; repeat to select multiple')
    parser.add_argument('--timeout', type=float, default=45.0)
    parser.add_argument('--max-mb', type=int, default=150)
    parser.add_argument('--dry-run', action='store_true', help='Print the official URLs without downloading')
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_mb <= 0:
        parser.error('--timeout and --max-mb must be positive')
    manifest = json.loads((ROOT / 'official_resources.json').read_text(encoding='utf-8'))
    entries = [b for b in manifest['books'] if b.get('pdf_url') and (not args.only or b['id'] in args.only)]
    statuses = []
    failed = False
    for book in entries:
        print(f"\n{book['title']}\n  Official page: {book['url']}\n  Download target: {book['pdf_url']}")
        if args.dry_run:
            continue
        record = {'id': book['id'], 'title': book['title'], 'source': book['pdf_url']}
        try:
            record.update(download_pdf(book['pdf_url'], args.out / book['filename'], args.timeout, args.max_mb * 1024 * 1024))
            print(f"  {record['status']}: {record['path']}")
        except Exception as exc:
            failed = True
            record.update(status='failed', error=f'{type(exc).__name__}: {exc}', manual_entry=book.get('reader_url', book['url']))
            print(f"  FAILED: {record['error']}\n  Browser entry: {record['manual_entry']}", file=sys.stderr)
        statuses.append(record)
    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        status_file = args.out / 'download_status.json'
        status_file.write_text(json.dumps({'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'results': statuses}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\nStatus report: {status_file}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
