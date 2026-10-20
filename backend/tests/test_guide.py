"""The user guide (docs/guide) must not drift from the code: generated references are current, every fault, scenario, message and command
is documented, every link and anchor resolves, and the worked scenario example really passes on both OCPP versions."""
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ocpp_simulator.faults import CATALOG
from ocpp_simulator.link import COMMAND_NAMES
from ocpp_simulator.scenarios import CATALOG_SCENARIOS
from ocpp_simulator.station import Station
from ocpp_simulator.v16 import CP_ACTIONS_16, COMMAND_NAMES_16

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "guide"
PAGES = sorted(GUIDE.glob("*.md")) + sorted((GUIDE / "reference").glob("*.md"))


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    out, in_code = set(), False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and (m := re.match(r"#{1,6}\s+(.*)", line)):
            out.add(slug(m.group(1)))
    return out


def test_the_ten_pages_and_references_exist():
    names = {p.name for p in GUIDE.glob("*.md")}
    assert {"README.md", "01-connect-and-versions.md", "02-sessions-and-cards.md", "03-metering-and-v2g.md", "04-faults-and-injection.md", "05-message-rules.md",
            "06-scenarios.md", "07-csms-commands.md", "08-tools.md", "09-api-cheatsheet.md", "10-recipes.md"} <= names
    assert {p.name for p in (GUIDE / "reference").glob("*.md")} == {"faults.md", "scenarios.md", "messages.md", "settings.md"}


def test_the_generated_reference_pages_are_current():
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_guide.py"), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_fault_scenario_message_and_command_is_documented():
    faults = (GUIDE / "reference" / "faults.md").read_text()
    scenarios = (GUIDE / "reference" / "scenarios.md").read_text()
    messages = (GUIDE / "reference" / "messages.md").read_text()
    assert [k for k in CATALOG if f"`{k}`" not in faults] == []
    assert [k for k in CATALOG_SCENARIOS if f"`{k}`" not in scenarios] == []
    names = set(Station.CP_ACTIONS) | CP_ACTIONS_16 | set(COMMAND_NAMES) | set(COMMAND_NAMES_16)
    assert sorted(n for n in names if f"`{n}`" not in messages) == []
    assert f"{len(CATALOG_SCENARIOS)} built-in scenarios" in (GUIDE / "06-scenarios.md").read_text()


def test_every_scenario_is_listed_in_the_scenarios_page_groups():
    page = (GUIDE / "06-scenarios.md").read_text()
    assert [k for k in CATALOG_SCENARIOS if f"`{k}`" not in page] == []


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(GUIDE)))
def test_links_and_anchors_resolve(page):
    text = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, frag = target.partition("#")
        dest = (page.parent / file_part).resolve() if file_part else page
        if not dest.exists():
            bad.append(f"{target}: no such file")
        elif frag and dest.suffix == ".md" and frag not in anchors(dest):
            bad.append(f"{target}: no such heading")
    assert bad == []


def test_every_mermaid_block_has_a_known_diagram_type_and_is_closed():
    for page in PAGES:
        text = page.read_text()
        assert text.count("```") % 2 == 0, f"{page.name}: unbalanced code fence"
        for block in re.findall(r"```mermaid\n(.*?)```", text, flags=re.S):
            assert block.split()[0] in ("flowchart", "sequenceDiagram", "stateDiagram-v2"), f"{page.name}: {block[:30]!r}"


def example_steps() -> list[dict]:
    text = (GUIDE / "06-scenarios.md").read_text()
    m = re.search(r"<!-- scenario-example -->\n```json\n(.*?)```", text, flags=re.S)
    assert m, "the worked scenario example is missing"
    return json.loads(m.group(1))


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
async def test_the_worked_scenario_example_passes(make_station, version):
    st = await make_station(ocpp_version=version)
    run = st.runner.start("guide_example", "Frozen meter is reported", example_steps())
    await asyncio.wait_for(st.runner.task, 120)
    assert run.status == "passed", [(s.label, s.status, s.detail) for s in run.steps if s.status not in ("passed", "skipped")]
