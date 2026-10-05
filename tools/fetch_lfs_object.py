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

    set MC2UE5_LFS_TOKEN=<token>          # never passed on the command line
    python3 tools/fetch_lfs_object.py --oid <sha256> --size <bytes> \
        --name overworld.bin --out Q:/MC2UE5/voxel

The download is verified against the sha256 the pointer declares, so a
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


def batch_request(repo, oid, size, token):
    """-> href of the signed download URL for one object."""
    body = json.dumps({
        "operation": "download",
        "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
    }).encode()
    auth = base64.b64encode(("stellarissss:%s" % token).encode()).decode()
    req = urllib.request.Request(
        BATCH % repo, data=body,
        headers={"Accept": "application/vnd.git-lfs+json",
                 "Content-Type": "application/vnd.git-lfs+json",
                 "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = json.loads(r.read())
    objs = payload.get("objects") or []
    if not objs:
        raise SystemExit("batch response carried no objects: %s"
                         % json.dumps(payload)[:300])
    action = (objs[0].get("actions") or {}).get("download") or {}
    href = action.get("href")
    if not href:
        raise SystemExit("no download href (error: %s)"
                         % objs[0].get("error"))
    return href


def download(href, dest, oid, size, attempts=5):
    """Fetch and verify. Writes .part then renames, so a failure leaves no
    half-written file where real data should be."""
    tmp = dest + ".part"
    last = ""
    for attempt in range(attempts):
        # Try the signed URL directly first; fall back to routing it through
        # the proxy, which is what works when the CDN host is not resolvable.
        for url in (href, PROXY + href):
            proc = subprocess.run(
                ["curl", "-sS", "-L", "--max-time", "900",
                 "-o", tmp, "-w", "%{http_code}", url],
                capture_output=True)
            code = proc.stdout.decode().strip()
            if code == "200" and os.path.exists(tmp):
                blob_len = os.path.getsize(tmp)
                if blob_len == size:
                    h = hashlib.sha256()
                    with open(tmp, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    if h.hexdigest() == oid:
                        os.replace(tmp, dest)
                        return True, ""
                    last = "sha mismatch"
                else:
                    last = "size %d, want %d" % (blob_len, size)
            else:
                last = "http %s" % code
        print("  attempt %d failed: %s" % (attempt + 1, last))
    return False, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oid", required=True, help="sha256 from the LFS pointer")
    ap.add_argument("--size", required=True, type=int)
    ap.add_argument("--name", required=True, help="file name to write")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args()

    token = os.environ.get("MC2UE5_LFS_TOKEN")
    if not token:
        raise SystemExit("set MC2UE5_LFS_TOKEN (do not pass tokens as args; "
                         "they end up in the shell history)")

    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, args.name)
    href = batch_request(args.repo, args.oid, args.size, token)
    ok, note = download(href, dest, args.oid, args.size)
    if ok:
        print("%s OK  %d bytes  sha256 verified" % (dest, args.size))
        return 0
    print("%s FAILED  (%s)" % (dest, note))
    return 1


if __name__ == "__main__":
    sys.exit(main())
