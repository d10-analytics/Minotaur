"""Run the installed ``minotaur`` console command as a module.

``python -m minotaur`` puts the working directory on ``sys.path`` (``''`` or
the absolute directory, plus any ``PYTHONPATH`` entry that names it), so a
project file such as ``json.py`` would shadow a module that Minotaur imports
and run before any Minotaur code. Before importing the CLI, the entry removes
every ``sys.path`` entry that resolves to the working directory. It uses only
``os`` and ``sys``, which the interpreter has already loaded; any other import
here would load project modules before the strip.

The package ``minotaur/__init__.py`` runs before this entry and must stay free
of imports for the same reason. CPython itself loads a few modules before the
entry file runs; those and a project-level ``minotaur`` module remain outside
this guard. The strip is a no-op on its own import: nothing changes unless the
file runs as ``__main__``.

Otherwise this module is a tiny delegation, so ``python -m minotaur`` does not
drift from the installed console script as command behavior evolves.
"""

import os
import sys

if __name__ == "__main__":
    try:
        _cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        pass  # Deleted working directory: nothing to strip, keep ``sys.path``.
    else:
        _kept: list[str] = []
        for _entry in sys.path:
            if isinstance(_entry, str):
                try:
                    if os.path.normcase(os.path.realpath(_entry)) == _cwd:
                        continue
                except (OSError, ValueError):
                    pass
            _kept.append(_entry)
        sys.path[:] = _kept

    from minotaur.cli import main

    raise SystemExit(main())
