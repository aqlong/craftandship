# monitor/cli.py - Monitor management CLI
#
# Usage:
#   python monitor/cli.py register <app_name> <live_url> --project <id> --service <id> --env <id>
#   python monitor/cli.py list
#   python monitor/cli.py check <app_id>
#   python monitor/cli.py diagnose <app_id>
#   python monitor/cli.py backup <app_id>
#   python monitor/cli.py restore <app_id> <backup_path>
#   python monitor/cli.py run

import argparse
import json
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_register(args) -> None:
    """Register a new app in the monitor registry."""
    from monitor.registry import register_app

    app_id = register_app(
        app_name=args.app_name,
        live_url=args.live_url,
        railway_project_id=args.project,
        railway_service_id=args.service,
        railway_environment_id=args.env,
    )
    print(f"Registered: {args.app_name} -> {app_id}")


def cmd_list(args) -> None:
    """List all registered apps."""
    from monitor.registry import list_apps

    apps = list_apps(status=getattr(args, "status", None))
    if not apps:
        print("No apps registered.")
        return

    print(f"{'ID':<38} {'Name':<25} {'Status':<10} {'URL'}")
    print("-" * 100)
    for app in apps:
        print(
            f"{app['id']:<38} {app['app_name']:<25} {app['status']:<10} {app['live_url']}"
        )


def cmd_check(args) -> None:
    """Run Tier 1+2 check for a single app."""
    from monitor.ping import check_app

    result = check_app(args.app_id)
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        print(f"App is DOWN. Run 'diagnose {args.app_id}' for deeper analysis.")
        sys.exit(1)


def cmd_diagnose(args) -> None:
    """Run full Tier 3+4 diagnosis for a single app."""
    from monitor.diagnose import run_diagnosis

    run_diagnosis(args.app_id)
    print("Diagnosis complete. Check logs for details.")


def cmd_backup(args) -> None:
    """Take a manual database snapshot for a single app."""
    from monitor.registry import get_app
    from monitor.backup import snapshot

    app = get_app(args.app_id)
    backup_path = snapshot(app, reason="manual")
    print(f"Backup saved: {backup_path}")


def cmd_restore(args) -> None:
    """Restore a database from a backup file."""
    from monitor.registry import get_app
    from monitor.backup import restore

    app = get_app(args.app_id)
    restore(app, args.backup_path)
    print(f"Restored {app['app_name']} from {args.backup_path}")


def cmd_run(args) -> None:
    """Start the continuous health check loop."""
    from monitor.ping import run_checks

    interval = getattr(args, "interval", 900)
    print(f"Starting health monitor (interval: {interval}s)...")
    run_checks(interval_seconds=interval)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python monitor/cli.py",
        description="Craft & Ship health monitor management CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # register
    reg = subparsers.add_parser("register", help="Register a new app")
    reg.add_argument("app_name", help="Human-readable app name")
    reg.add_argument("live_url", help="Live HTTPS URL of the app")
    reg.add_argument("--project", required=True, help="Railway project ID")
    reg.add_argument("--service", required=True, help="Railway service ID")
    reg.add_argument("--env", required=True, help="Railway environment ID")

    # list
    lst = subparsers.add_parser("list", help="List registered apps")
    lst.add_argument(
        "--status",
        choices=["healthy", "degraded", "down", "unknown"],
        help="Filter by status",
    )

    # check
    chk = subparsers.add_parser("check", help="Run Tier 1+2 check for an app")
    chk.add_argument("app_id", help="App UUID from registry")

    # diagnose
    diag = subparsers.add_parser("diagnose", help="Run full Tier 3+4 diagnosis")
    diag.add_argument("app_id", help="App UUID from registry")

    # backup
    bkp = subparsers.add_parser("backup", help="Take a manual database snapshot")
    bkp.add_argument("app_id", help="App UUID from registry")

    # restore
    rst = subparsers.add_parser("restore", help="Restore a database from backup")
    rst.add_argument("app_id", help="App UUID from registry")
    rst.add_argument("backup_path", help="Path to the .sql.gz backup file")

    # run
    run = subparsers.add_parser("run", help="Start continuous check loop")
    run.add_argument(
        "--interval",
        type=int,
        default=900,
        help="Seconds between check cycles (default: 900)",
    )

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    handlers = {
        "register": cmd_register,
        "list": cmd_list,
        "check": cmd_check,
        "diagnose": cmd_diagnose,
        "backup": cmd_backup,
        "restore": cmd_restore,
        "run": cmd_run,
    }

    try:
        handlers[args.command](args)
    except KeyError as error:
        print(f"Error: app not found: {error}")
        sys.exit(1)
    except RuntimeError as error:
        print(f"Error: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
