"""
main.py
Usage: 
    PYTHONUNBUFFERED=1 python main.py create   --project my-shop   --request "Create a user-service (login, register) and an order-service (create order). Order-service must validate user via user-service before processing orders."   > log.txt 2>&1
    python main.py modify --project my-shop --request "Add order history method to OrderService" 
    python main.py status --project my-shop python main.py skill list 
    python main.py skill run create_spring_boot_module --project my-shop --params '{"module_name":"service-user","port":8051}'
"""

from __future__ import annotations
import json
import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

load_dotenv()

from crewai import LLM
import os


def get_llm():
    return LLM(
        model=os.getenv("OPENAI_MODEL_NAME", "deepseek-ai/DeepSeek-V3"),
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL"),
        temperature=0.2,
    )


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/app.log") if Path("logs").exists() else logging.NullHandler(),
    ],
)

console = Console()

# Ensure project packages are importable
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# CLI groups
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """🤖 CrewAI Microservice Development Assistant"""
    pass


# ---------------------------------------------------------------------------
# create / modify
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--project", "-p", required=True)
@click.option("--request", "-r", required=True)
def create(project: str, request: str):
    """Create microservice"""
    _run_crew(request=request, project_name=project)


@cli.command()
@click.option("--project", "-p", required=True)
@click.option("--request", "-r", required=True)
def modify(project: str, request: str):
    """Modify microservice"""
    _run_crew(request=request, project_name=project)


def _run_crew(request: str, project_name: str):
    from crew import run_crew
    from project_context import GlobalNacosState

    GlobalNacosState.get()  # 内部会做 docker inspect 兜底

    console.print(
        Panel(
            f"[bold cyan]Project:[/] {project_name}\n"
            f"[bold cyan]Request:[/] {request}\n",
            title="🚀 Starting Microservice Builder",
            border_style="cyan",
        )
    )

    try:
        result = run_crew(request, project_name, {"llm": get_llm()})
        console.print(Panel(str(result), title="✅ Crew Result", border_style="green"))

    except KeyboardInterrupt:
        console.print("\n[yellow]Aborted by user.[/yellow]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        logging.exception("Crew execution failed")
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--project", "-p", required=True)
def status(project: str):
    from project_context import ContextRegistry

    projects_root = os.getenv("PROJECTS_ROOT", "./output")
    ctx = ContextRegistry.get_or_create(project, projects_root)
    ctx.scan_existing_project()
    console.print(ctx.to_summary())
    ContextRegistry.close_all()


# ---------------------------------------------------------------------------
# skill group
# ---------------------------------------------------------------------------

@cli.group()
def skill():
    pass


@skill.command("list")
def skill_list():
    from tools.skill_runner import list_skills

    table = Table(title="Available Skills", show_header=True)
    table.add_column("Skill Name", style="cyan")
    table.add_column("SKILL.md")

    for s in list_skills():
        skill_dir = Path("skills") / s
        has_doc = "✓" if (skill_dir / "SKILL.md").exists() else "✗"
        table.add_row(s, has_doc)

    console.print(table)


@skill.command("run")
@click.argument("skill_name")
@click.option("--project", "-p", required=True)
@click.option("--params", default="{}")
def skill_run(skill_name: str, project: str, params: str):
    from project_context import ContextRegistry
    from tools.skill_runner import run_skill

    try:
        p = json.loads(params)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON params:[/red] {e}")
        sys.exit(1)

    projects_root = os.getenv("PROJECTS_ROOT", "./output")
    ctx = ContextRegistry.get_or_create(project, projects_root)

    console.print(f"Running skill {skill_name}")
    result = run_skill(skill_name, p, ctx)

    syntax = Syntax(json.dumps(result, indent=2), "json", theme="monokai")
    console.print(Panel(syntax, title="Skill Result"))

    ContextRegistry.close_all()


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--project", "-p", required=True)
@click.option("--module", "-m", default="")
def validate(project: str, module: str):
    from project_context import ContextRegistry
    from tools.validator import MavenValidator

    projects_root = os.getenv("PROJECTS_ROOT", "./output")
    ctx = ContextRegistry.get_or_create(project, projects_root)

    validator = MavenValidator(ctx.project_root)

    console.print(f"Compiling {project}...")

    result = validator.compile(module=module or None)

    if result["success"]:
        console.print("[green]✅ Build SUCCESS[/green]")
    elif result["success"] is None:
        console.print("[yellow]⚠ mvn not found[/yellow]")
    else:
        console.print("[red]❌ Build FAILED[/red]")
        for err in result["errors"]:
            console.print(f"  {err}")

    ContextRegistry.close_all()


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--project", "-p", required=True)
def docker(project: str):
    from project_context import ContextRegistry
    from tools.skill_runner import run_skill

    projects_root = os.getenv("PROJECTS_ROOT", "./output")
    ctx = ContextRegistry.get_or_create(project, projects_root)

    result = run_skill("update_docker_compose", {}, ctx)
    console.print(json.dumps(result, indent=2))

    output_root = Path(projects_root) / project

    console.print("\n[bold]Start services:[/bold]")
    console.print(f"  cd {output_root}")
    console.print("  docker compose up -d")

    ContextRegistry.close_all()


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    cli()