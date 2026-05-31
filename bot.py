"""Tranger Cloud entry point."""
import logging
import sys

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

if __name__ == "__main__":
    try:
        from script import main

        main()
    except SystemExit:
        raise
    except Exception:
        logging.exception("LedgerPilot crashed on startup")
        sys.exit(1)
