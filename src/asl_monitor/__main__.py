"""Start the MCP server over stdio: ``python -m asl_monitor``."""

from __future__ import annotations


def main() -> None:
    from .server import mcp

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
