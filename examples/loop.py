"""The CLI loop holds the same OS lock across all processing and waits."""
from clipforge.worker import main

if __name__ == "__main__":
    main(["--config", "/absolute/path/to/config.json", "loop", "--interval", "60", "--discovery"])
