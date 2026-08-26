from pathlib import Path

from packaging.requirements import Requirement


def test_mcp_dependency_stays_on_decorator_compatible_major_version():
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    mcp_requirement = next(
        Requirement(line)
        for line in requirements.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and Requirement(line).name == "mcp"
    )

    assert mcp_requirement.specifier.contains("1.29.1")
    assert not mcp_requirement.specifier.contains("2.0.0")
