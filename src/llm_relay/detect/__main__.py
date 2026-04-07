"""python -m llm_relay.detect entry point."""

from __future__ import annotations


def main() -> None:
    try:
        from llm_relay.detect.cli import main as cli_main

        cli_main()
    except ImportError:
        from llm_relay.detect._fallback_cli import main as fallback_main

        fallback_main()


if __name__ == "__main__":
    main()
