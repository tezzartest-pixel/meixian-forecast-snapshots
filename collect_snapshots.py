"""Fetch the official Mei County forecast pages and station observations and store them as gzip snapshots.

Runs inside GitHub Actions several times a day (see .github/workflows/collect.yml). It needs only the Python
standard library and never parses the pages: parsing happens on the desktop program after synchronisation,
so this script does not have to change when the parsers change.

Repository layout written by this script (consumed by fcverify/cloudsync.py on the desktop):
    snapshots/YYYY/MM/DD/<source>_<HHMMSS>.<ext>.gz   raw HTTP body, gzip; time is Beijing time
    index/YYYY-MM.json                                {"files": [{"path", "source", "time_bjt", "bytes"}, ...]}
    latest.json                                       {"months", "last_run_utc", "last_snapshot_bjt", "results"}

Exit status is 1 when both nmc.cn sources failed (GitHub then sends a failure notification), 0 otherwise.
"""
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

STATION_ID = os.environ.get("NMC_STATION_ID", "sczdz")
NMC_PAGE_URL = os.environ.get("NMC_PAGE_URL", "http://www.nmc.cn/publish/forecast/ASN/meixian.html")
CMA_CODE = os.environ.get("CMA_STATION_CODE", "101110908")
ROOT = os.environ.get("SNAPSHOT_ROOT", ".")

# source name, file extension, url, validator (a substring that a genuine page contains)
SOURCES = [
    ("nmc_rest", "json", f"http://www.nmc.cn/rest/weather?stationid={STATION_ID}", '"predict"'),
    ("nmc_page", "html", NMC_PAGE_URL, "hourValues"),
    ("cma_7d", "html", f"https://www.weather.com.cn/weather/{CMA_CODE}.shtml", "fc_24h_internal_update_time"),
    ("cma_15d", "html", f"https://www.weather.com.cn/weather15d/{CMA_CODE}.shtml", 't clearfix'),
]
REQUIRED = ("nmc_rest", "nmc_page")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BJT = timezone(timedelta(hours=8))


def fetch(url: str, attempts: int = 3, timeout: int = 40) -> bytes:
    """GET a URL with retries. SNAPSHOT_NO_PROXY=1 bypasses any proxy (used for local tests on the desktop)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + "/"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if os.environ.get("SNAPSHOT_NO_PROXY") else urllib.request.build_opener()
    last = None
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(3 + 5 * attempt)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def validate(source: str, body: bytes, marker: str) -> None:
    text = body.decode("utf-8", "replace")
    if source == "nmc_rest":
        data = json.loads(text)
        if not isinstance(data, dict) or not data.get("data") or not data["data"].get("predict", {}).get("publish_time"):
            raise ValueError("unexpected JSON structure")
    if marker not in text:
        raise ValueError(f"marker {marker!r} not found in the page")


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    now_bjt = now_utc.astimezone(BJT).replace(tzinfo=None)
    day_dir = os.path.join(ROOT, "snapshots", now_bjt.strftime("%Y"), now_bjt.strftime("%m"), now_bjt.strftime("%d"))
    month_index_path = os.path.join(ROOT, "index", now_bjt.strftime("%Y-%m") + ".json")
    month_index = load_json(month_index_path, {"files": []})
    results, written = {}, []
    for source, ext, url, marker in SOURCES:
        try:
            body = fetch(url)
            validate(source, body, marker)
        except Exception as exc:  # noqa: BLE001 - every source is independent
            results[source] = f"error: {exc}"[:300]
            print(f"{source}: {results[source]}")
            continue
        name = f"{source}_{now_bjt.strftime('%H%M%S')}.{ext}.gz"
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, name)
        with gzip.open(path, "wb") as fh:
            fh.write(body)
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        entry = {"path": rel, "source": source, "time_bjt": now_bjt.strftime("%Y-%m-%d %H:%M:%S"), "bytes": len(body)}
        month_index["files"].append(entry)
        written.append(entry)
        results[source] = "ok"
        print(f"{source}: ok, {len(body)} bytes -> {rel}")

    if written:
        month_index["files"].sort(key=lambda item: (item["time_bjt"], item["source"]))
        dump_json(month_index_path, month_index)
    index_dir = os.path.join(ROOT, "index")
    months = sorted(name[:-5] for name in os.listdir(index_dir)) if os.path.isdir(index_dir) else []
    latest_path = os.path.join(ROOT, "latest.json")
    previous = load_json(latest_path, {})
    last_snapshot = max([item["time_bjt"] for item in written] + [previous.get("last_snapshot_bjt") or ""]) or None
    dump_json(latest_path, {
        "months": months,
        "last_run_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_snapshot_bjt": last_snapshot,
        "results": results,
    })
    failed_required = [source for source in REQUIRED if results.get(source) != "ok"]
    if failed_required:
        print("required sources failed:", ", ".join(failed_required))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
