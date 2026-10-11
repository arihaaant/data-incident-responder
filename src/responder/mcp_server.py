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
    def list_tables(schema: str | None = None) -> str:
        """Tables and views with their columns and types. Pass a schema to keep it short: raw (landed source
        data), staging (cleaned) or marts (business tables)."""
        return json.dumps(ws.list_tables(schema))

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

    @mcp.tool()
    def profile_column(table: str, column: str, target_type: str = "") -> str:
        """Check whether a raw column converts to the type its staging contract expects (e.g. lineitem,
        l_discount) without losing anything: values that fail to convert, values a conversion would round,
        decimal places the source no longer sends, and for text dates the format that parses them. The type is
        read from the staging contract; target_type is only used for a column no contract covers."""
        return json.dumps(ws.profile_column(table, column, target_type), default=str)

    @mcp.tool()
    def try_patch(file: str, sql: str) -> str:
        """Test a fix before proposing it: builds the warehouse in a throwaway copy with one model file replaced
        (e.g. models/staging/stg_orders.sql) and reports any failing checks or build errors."""
        return json.dumps(ws.try_patch(file, sql))

    return mcp


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    args = p.parse_args(argv)
    make_server(args.project).run()


if __name__ == "__main__":
    main()
