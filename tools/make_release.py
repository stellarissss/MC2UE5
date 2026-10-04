# -*- coding: utf-8 -*-
"""
make_release.py -- pack the staged build into a release archive.

Deliberately uses Python's ``tarfile`` rather than shelling out to ``tar``:
Git Bash on Windows rewrites ``Q:/...`` into a path its own ``tar`` then
mis-parses ("Cannot write: Broken pipe"), and the archive has to be byte-exact
regardless of which shell is driving the build.

Produces a gzip tarball with the staged build at the archive root, so
extracting it yields a runnable directory rather than a nested one.

    python3 make_release.py --dist <staged build dir> --out <archive.tar.gz>
"""

import argparse
import os
import sys
import tarfile
import time


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True,
                    help="directory holding the staged build (the folder that "
                         "contains the .exe)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", default="MC2UE5",
                    help="top-level directory inside the archive")
    args = ap.parse_args()

    dist = os.path.abspath(args.dist)
    if not os.path.isdir(dist):
        sys.stderr.write("make_release: no such directory: %s\n" % dist)
        return 1
    exe = os.path.join(dist, "MCReplica.exe")
    if not os.path.isfile(exe):
        sys.stderr.write("make_release: %s has no MCReplica.exe\n" % dist)
        return 1

    t0 = time.time()
    files = 0
    total = 0
    print("packing %s -> %s" % (dist, args.prefix))
    sys.stdout.flush()

    with tarfile.open(args.out, "w:gz", compresslevel=6) as tar:
        for dirpath, dirnames, filenames in os.walk(dist):
            dirnames.sort()
            filenames.sort()
            rel = os.path.relpath(dirpath, dist)
            for name in filenames:
                full = os.path.join(dirpath, name)
                if not os.path.isfile(full):
                    continue
                arc = os.path.join(args.prefix, name) if rel == "." else \
                    os.path.join(args.prefix, rel, name)
                tar.add(full, arcname=arc, recursive=False)
                files += 1
                total += os.path.getsize(full)
            if files and files % 2000 == 0:
                print("  ... %d files" % files)
                sys.stdout.flush()

    size = os.path.getsize(args.out)
    print("=" * 60)
    print("%d files, %s -> %s (%.0fs)"
          % (files, human(total), human(size), time.time() - t0))

    # Verify the archive reads back and the executable is inside it, so a
    # truncated or malformed tarball cannot be uploaded as a release.
    with tarfile.open(args.out, "r:gz") as tar:
        names = tar.getnames()
    want = "%s/MCReplica.exe" % args.prefix.replace("\\", "/")
    if want not in names:
        sys.stderr.write("make_release: %s missing from the archive\n" % want)
        return 1
    print("verified: %d entries, %s present" % (len(names), want))
    return 0


if __name__ == "__main__":
    sys.exit(main())
