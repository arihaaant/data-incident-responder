"""MCP server over one incident workspace.

    python -m responder.mcp_server --project kit/work/inc001
"""
import argparse
import json

from mcp.server.fastmcp import FastMCP

from .tools import Workspace


def make_server(project):
    ws = Workspace(project)
    mcp = FastMCP("warehouse")

    @mcp.tool()
    def list_tables() -> str:
        """Every table and view in the warehouse with its columns and types. Schemas: raw (landed source
        data), staging (cleaned), marts (business tables)."""
        return json.dumps(ws.list_tables())

    @mcp.tool()
    def run_sql(query: str) -> str:
        """Run one read-only SELECT against the warehouse (DuckDB). At most 50 rows come back and personal
        columns are masked. Anything that writes or reads files is refused."""
        return json.dumps(ws.run_sql(query))

    @mcp.tool()
    def get_lineage(name: str) -> str:
        """Upstream and downstream nodes of a dbt model or source (e.g. stg_orders, fct_orders, raw.orders),
        plus the checks attached to it."""
        return json.dumps(ws.get_lineage(name))

    @mcp.tool()
    def get_test_results() -> str:
        """The checks that failed or errored in the last dbt build, with their messages."""
        return json.dumps(ws.get_test_results())

    @mcp.tool()
    def get_model_sql(name: str) -> str:
        """The SQL source of a dbt model, e.g. stg_orders."""
        return json.dumps(ws.get_model_sql(name))

    return mcp


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    args = p.parse_args(argv)
    make_server(args.project).run()


if __name__ == "__main__":
    main()
