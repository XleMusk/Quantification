"""Fetch the selected official notebooks and lecture notes, without executing them."""

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
COMMIT = "a892791ce5a3831134cfa2989033ecbb9289f1ea"
RAW = f"https://raw.githubusercontent.com/wesm/pydata-book/{COMMIT}/"
ASSETS = [{"path": f"code/pydata-book/{name}", "url": RAW + name, "kind": "notebook" if name.endswith(".ipynb") else "text"}
          for name in ["ch05.ipynb", "ch06.ipynb", "ch07.ipynb", "ch08.ipynb", "ch10.ipynb", "ch11.ipynb", "COPYING", "README.md"]]
ASSETS += [
    {"path": "courses/MIT15401_Equities.pdf", "kind": "pdf", "url": "https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/1aea40b063cad5a335e980ecf30d84b7_MIT15_401F08_lec07.pdf"},
    {"path": "courses/MIT15401_Risk_and_Return.pdf", "kind": "pdf", "url": "https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/637f2121ff813c0385c42c8f7d376761_MIT15_401F08_lec12.pdf"},
    {"path": "courses/MIT1805_Covariance_and_Correlation.pdf", "kind": "pdf", "url": "https://ocw.mit.edu/courses/18-05-introduction-to-probability-and-statistics-spring-2022/mit18_05_s22_class07-prep-b.pdf"},
]


def validate(path, kind):
    if path.stat().st_size == 0:
        raise ValueError("Empty file")
    extra = {}
    if kind == "pdf":
        with path.open("rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                raise ValueError("Response is not a PDF")
        reader = PdfReader(path)
        extra["pages"] = len(reader.pages)
        if not extra["pages"]:
            raise ValueError("No PDF pages")
        extra["first_page_text_present"] = bool(reader.pages[0].extract_text().strip())
    elif kind == "notebook":
        nb = json.loads(path.read_text(encoding="utf-8"))
        if nb.get("nbformat") != 4 or not isinstance(nb.get("cells"), list):
            raise ValueError("Response is not a notebook")
        extra["cells"] = len(nb["cells"])
        extra["execution"] = "not_run; upstream outputs are not local execution evidence"
    else:
        body = path.read_text(encoding="utf-8")
        if "<html" in body[:200].lower() or "<!doctype" in body[:200].lower():
            raise ValueError("Unexpected HTML response")
    extra.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return extra


def fetch(asset):
    target = ROOT / asset["path"]
    partial = target.with_suffix(target.suffix + ".part")
    record = dict(asset)
    created = False
    try:
        if target.exists():
            record.update(validate(target, asset["kind"]), status="existing_validated")
            return record
        target.parent.mkdir(parents=True, exist_ok=True)
        if partial.exists():
            raise FileExistsError(f"Inspect existing partial file: {partial}")
        created = True
        command = ["curl.exe", "--location", "--fail", "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "40",
                   "--max-filesize", str(30 * 1024 * 1024), "--output", str(partial), asset["url"]]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        record.update(validate(partial, asset["kind"]))
        partial.rename(target)
        record["status"] = "downloaded"
    except Exception as error:
        if created:
            partial.unlink(missing_ok=True)
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
    return record


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(fetch, ASSETS))
    payload = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "pydata_branch": "3rd-edition", "pydata_commit": COMMIT, "results": results}
    (ROOT / "starter_download_status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in results:
        print(item["status"], item["path"], item.get("bytes", item.get("error", "")))
    raise SystemExit(int(any(item["status"] == "failed" for item in results)))
