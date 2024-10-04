import tomli
import yaml

with open("conda-store-server/pyproject.toml", "rb") as f:
    pyproject = set(tomli.load(f)["project"]["dependencies"])

with open("conda-store-server/environment-dev.yaml", "r") as f:
    environment = yaml.load(f, Loader=yaml.SafeLoader)["dependencies"]

    conda = set()
    other_tools = {}
    for item in environment:
        if isinstance(item, dict):
            if other_tools:
                # There should only be one dictionary in this list of dependencies
                raise ValueError(
                    "Found multiple other tool dictionaries in environment-dev.yaml dependencies"
                )

            other_tools = item
        else:
            conda.add(item)

    # Convert all tool dependencies to sets for using set algebra later
    for tool, dependencies in other_tools.items():
        other_tools[tool] = set(dependencies)

with open(".python-version-default", "r") as f:
    default_python = f.read().strip()

# Check the default python version matches
assert f"python={default_python}" in conda

breakpoint()

# Check that the python dependencies in pyproject.toml are in either the conda deps or the pip deps
assert pyproject - (conda + other_tools.get("pip", set())) == set()
