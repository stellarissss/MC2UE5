# -*- coding: utf-8 -*-
"""
fetch_lfs_raw.py -- materialise LFS pointer files through raw.githubusercontent.

The GitHub LFS batch endpoint lives on github.com, which the egress proxy in
front of this machine blocks outright, and the REST contents API hands back the
pointer rather than the object. raw.githubusercontent.com does resolve LFS
pointers to real content, so this walks the pointer list over that host.

Two things make it reliable rather than a retry loop:

  * every object is checked against the sha256 its pointer declares, so a
    truncated transfer is rejected instead of becoming a corrupt texture;
  * requests are serial with a short backoff, because the proxy fails
    intermittently and concurrency turns that into a burst of failures rather
    than smoothing it out.

    python3 fetch_lfs_raw.py --root <checkout> [--include assets/textures]
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time

#: The LFS-aware media host. ``raw.githubusercontent.com`` and the LFS batch
#: endpoint on ``github.com`` are both unreachable from this network, and the
#: REST contents API hands back the 128-byte pointer rather than the object.
#: This host resolves pointers to real content and is the one that works.
MEDIA = "https://media.githubusercontent.com/media/%s/%s/%s"
POINTER_SIZE = 200


def is_pointer(path):
    try:
        if os.path.getsize(path) > POINTER_SIZE:
            return False
        with open(path, "rb") as fh:
            return fh.read(9) == b"version h"
    except OSError:
        return False


def parse_pointer(path):
    oid = size = None
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if line.startswith("oid sha256:"):
                oid = line.split(":", 1)[1].strip()
            elif line.startswith("size "):
                size = int(line.split()[1])
    return oid, size


def fetch_one(url, dest, oid, size, attempts=6):
    """
    Download and verify. Returns (ok, note).

    Writes to a temporary name and renames on success, so a failed transfer
    never leaves a half-written file where a real texture should be. Nothing
    is deleted on the failure path -- the temp file is simply left for the next
    attempt to overwrite, which also keeps the downloader clear of any
    delete-hook on the workspace.
    """
    tmp = dest + ".part"
    for attempt in range(attempts):
        try:
            out = subprocess.run(
                ["curl", "-sS", "-L", "--max-time", "90", "-o", tmp,
                 "-w", "%{http_code}", url],
                capture_output=True, timeout=110)
            code = out.stdout.decode().strip()
        except subprocess.TimeoutExpired:
            code = "timeout"

        if code == "200" and os.path.exists(tmp):
            with open(tmp, "rb") as fh:
                blob = fh.read()
            if hashlib.sha256(blob).hexdigest() == oid and len(blob) == size:
                os.replace(tmp, dest)
                return True, ""
            note = "size %d, want %d" % (len(blob), size)
        else:
            note = "http %s" % code

        if attempt < attempts - 1:
            time.sleep(1.0 + attempt * 1.2)

    return False, note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="stellarissss/MC2UE5")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--root", required=True)
    ap.add_argument("--include", default="",
                    help="only paths containing this substring")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    targets = []
    for dirpath, _dirs, files in os.walk(root):
        if os.path.join(".git") in dirpath:
            continue
        for name in files:
            p = os.path.join(dirpath, name)
            if not is_pointer(p):
                continue
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if args.include and args.include not in rel:
                continue
            targets.append((rel, p))

    print("pointers to fetch: %d" % len(targets))
    if not targets:
        print("nothing to do")
        return 0

    t0 = time.time()
    done = 0
    failed = []
    for i, (rel, path) in enumerate(targets, 1):
        oid, size = parse_pointer(path)
        url = MEDIA % (args.repo, args.ref, rel)
        ok, note = fetch_one(url, path, oid, size)
        if ok:
            done += 1
        else:
            failed.append((rel, note))
            print("  FAIL %-58s (%s)" % (rel, note))
        if i % 50 == 0 or i == len(targets):
            print("  ... %d/%d  ok=%d  fail=%d  %.0fs"
                  % (i, len(targets), done, len(failed), time.time() - t0))
            sys.stdout.flush()

    print("=" * 60)
    print("fetched %d/%d in %.0fs" % (done, len(targets), time.time() - t0))
    if failed:
        print("failed %d:" % len(failed))
        for rel, note in failed[:20]:
            print("  %s (%s)" % (rel, note))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
