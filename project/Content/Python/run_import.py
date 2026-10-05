# -*- coding: utf-8 -*-
"""Run import_world.run() from -ExecutePythonScript (import_world has no __main__)."""
import traceback
try:
    import import_world
    import_world.run()
except Exception:
    open("Q:/MC2UE5/logs/run_import_err.txt", "w").write(traceback.format_exc())
    raise
