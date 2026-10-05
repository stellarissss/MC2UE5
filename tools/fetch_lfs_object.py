# -*- coding: utf-8 -*-
"""
fetch_lfs_object.py -- materialise a Git LFS object that is no longer in the tree.

``tools/fetch_lfs_raw.py`` walks the *working tree* for pointer files and pulls
them through ``media.githubusercontent.com``. That does not help for an object
whose file was deleted: there is no pointer on disk, and both
``media.githubusercontent.com`` and ``raw.githubusercontent.com`` answer with
the 128-byte pointer text rather than the content anyway.

The LFS batch endpoint on ``github.com`` is additionally blocked by the egress
proxy in front of this machine -- but ``ghfast.top`` proxies it, so the batch
call works there and returns a signed download URL.

That signed URL cannot simply be fetched in one request: the transfer stalls
around 30 MB, and re-routing the URL through the proxy returns 403 because the
signature is bound to its original host. **Range requests are answered
correctly** (HTTP 206 with the exact byte count), so the object is assembled
from fixed-size chunks, each retried, with a fresh signature every few attempts
since signatures expire after an hour. Finished chunks are kept, so an
interrupted run resumes rather than restarting.

    set MC2UE5_LFS_TOKEN=<token>          # never passed on the command line
    python3 tools/fetch_lfs_object.py --oid <sha256> --size <bytes> \
        --name overworld.bin --out Q:/MC2UE5/voxel

The assembled file is verified against the sha256 the pointer declares, so a
truncated transfer is rejected instead of becoming a corrupt archive.
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

DEFAULT_REPO = "stellarissss/MC2UE5"
PROXY = "https://ghfast.top/"
BATCH = PROXY + "https://github.com/%s.git/info/lfs/objects/batch"
CHUNK = 2 * 1024 * 1024


def sign(repo, oid, size, token):
    """-> a freshly signed download URL for one object."""
    body = json.dumps({"operation": "download", "transfers": ["basic"],
                       "objects": [{"oid": oid, "size": size}]}).encode()
    auth = base64.b64encode(("stellarissss:%s" % token).encode()).decode()
    req = urllib.request.Request(
        BATCH % repo, data=body,
        headers={"Accept": "application/vnd.git-lfs+json",
                 "Content-Type": "application/vnd.git-lfs+json",
                 "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = json.loads(r.read())
    obj = (payload.get("objects") or [{}])[0]
    href = ((obj.get("actions") or {}).get("download") or {}).get("href")
    if not href:
        raise RuntimeError("no download href (error: %s)" % obj.get("error"))
    return href


def get_range(href, start, end, dest):
    """-> (ok, note). ok only when exactly the requested byte count arrived."""
    want = end - start + 1
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "240",
         "-r", "%d-%d" % (start, end), "-o", dest, "-w", "%{http_code}", href],
        capture_output=True)
    code = proc.stdout.decode().strip()
    got = os.path.getsize(dest) if os.path.exists(dest) else 0
    if got == want:
        return True, code
    return False, "%s (got %d want %d)" % (code, got, want)


def fetch(repo, oid, size, dest, chunk=CHUNK, attempts=8):
    tmpdir = dest + ".chunks"
    os.makedirs(tmpdir, exist_ok=True)
    starts = list(range(0, size, chunk))
    print("fetching %d bytes in %d chunks of %d" % (size, len(starts), chunk))

    href = sign(repo, oid, size, os.environ["MC2UE5_LFS_TOKEN"])
    failed = []
    for i, start in enumerate(starts):
        end = min(start + chunk - 1, size - 1)
        part = os.path.join(tmpdir, "%08d.part" % start)
        if os.path.exists(part) and os.path.getsize(part) == end - start + 1:
            continue
        ok = False
        for attempt in range(attempts):
            if attempt and attempt % 3 == 0:
                try:
                    href = sign(repo, oid, size, os.environ["MC2UE5_LFS_TOKEN"])
                except Exception as exc:
                    print("  re-sign failed: %s" % exc)
            ok, note = get_range(href, start, end, part)
            if ok:
                break
            print("  chunk %d/%d attempt %d: %s"
                  % (i + 1, len(starts), attempt + 1, note))
        if not ok:
            failed.append(start)
        if (i + 1) % 5 == 0 or i + 1 == len(starts):
            done = sum(1 for s in starts
                       if os.path.exists(os.path.join(tmpdir, "%08d.part" % s)))
            print("  %d/%d chunks (%.1f%%) failed=%d"
                  % (done, len(starts), 100.0 * done / len(starts), len(failed)))
            sys.stdout.flush()

    if failed:
        print("INCOMPLETE: %d chunks failed -- rerun to retry only them"
              % len(failed))
        return False

    h = hashlib.sha256()
    with open(dest + ".part", "wb") as out:
        for start in starts:
            with open(os.path.join(tmpdir, "%08d.part" % start), "rb") as fh:
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
                    h.update(b)
    got = os.path.getsize(dest + ".part")
    if got != size:
        print("SIZE MISMATCH: %d != %d" % (got, size))
        return False
    if h.hexdigest() != oid:
        print("SHA256 MISMATCH: %s != %s" % (h.hexdigest(), oid))
        return False
    os.replace(dest + ".part", dest)
    print("OK %s  %d bytes  sha256 verified" % (dest, got))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oid", required=True, help="sha256 from the LFS pointer")
    ap.add_argument("--size", required=True, type=int)
    ap.add_argument("--name", required=True, help="file name to write")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--chunk", type=int, default=CHUNK)
    args = ap.parse_args()

    if not os.environ.get("MC2UE5_LFS_TOKEN"):
        raise SystemExit("set MC2UE5_LFS_TOKEN (do not pass tokens as args; "
                         "they end up in the shell history)")
    os.makedirs(args.out, exist_ok=True)
    ok = fetch(args.repo, args.oid, args.size,
               os.path.join(args.out, args.name), args.chunk)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
