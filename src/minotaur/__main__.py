"""Run the installed ``minotaur`` console command as a module.

Keeping this module as a tiny delegation prevents ``python -m minotaur`` from
drifting from the installed console script as command behavior evolves.
"""

from minotaur.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
