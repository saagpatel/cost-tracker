"""Entry point for running cost-tracker as a module."""

from cost_tracker.server import app


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
