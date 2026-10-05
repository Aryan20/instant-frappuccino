import sys


def main() -> int:
    if "--diagnose-actions" in sys.argv:
        from fmapp.diagnose import run_actions

        args = sys.argv[sys.argv.index("--diagnose-actions") + 1 :]
        return run_actions(*args[:2])
    if "--diagnose" in sys.argv:
        from fmapp.diagnose import run as diagnose

        return diagnose()
    from fmapp.ui.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
