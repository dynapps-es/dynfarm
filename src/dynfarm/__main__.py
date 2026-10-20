"""`python -m dynfarm`: the same command as the `dynfarm` script.

It is what works when the directory pip puts scripts in is not on `PATH` (the usual state of a fresh Python on
Windows: `py -m pip install dynfarm`, then `py -m dynfarm --version`), and what a script can call without guessing
where the interpreter's `Scripts` or `bin` directory is. The program name stays `dynfarm` in `--help` and in errors.
"""

from dynfarm.cli import main

if __name__ == "__main__":
    main()
