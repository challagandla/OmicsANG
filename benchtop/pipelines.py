# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Pipeline discovery + Snakemake command construction.

Scans the root directory for pipeline repos and figures out, per repo:
  * workflow engine (snakemake / python / shell)
  * Snakefile location (prefers workflow/Snakefile, falls back to ./Snakefile)
  * main config + any project_local/*.yaml overrides (your convention)
  * Snakemake profiles
  * conda env name (parsed from environment.yml / envs/*.yml) so runs launch
    *inside* the right env even though snakemake isn't on the base PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import settings


@dataclass
class Pipeline:
    name: str
    path: Path
    kind: str  # snakemake | python | shell | unknown
    snakefile: Optional[str] = None  # relative to path
    configs: list[str] = field(default_factory=list)  # relative
    project_local: list[str] = field(default_factory=list)  # relative
    test_configs: list[str] = field(default_factory=list)  # .test bundled configs
    profiles: list[str] = field(default_factory=list)
    conda_env: Optional[str] = None
    env_file: Optional[str] = None
    readme: Optional[str] = None
    entrypoint: Optional[str] = None  # for python/shell pipelines

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "local_name": self.path.name,
            "path": str(self.path),
            "kind": self.kind,
            "snakefile": self.snakefile,
            "configs": self.configs,
            "project_local": self.project_local,
            "test_configs": self.test_configs,
            "profiles": self.profiles,
            "conda_env": self.conda_env,
            "env_file": self.env_file,
            "readme": self.readme,
            "entrypoint": self.entrypoint,
        }


def _conda_env_name(env_file: Path) -> Optional[str]:
    try:
        data = yaml.safe_load(env_file.read_text())
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
    except Exception:
        pass
    return None


def _env_mentions_snakemake(env_file: Path) -> bool:
    try:
        return "snakemake" in env_file.read_text().lower()
    except Exception:
        return False


def _valid_pipeline_id(value: object) -> bool:
    """Return whether *value* is a safe, stable direct-child identifier.

    Pipeline IDs are local directory names.  They intentionally never derive
    from Git remotes: two local checkouts may share an upstream repository, and
    a remote is attacker-controlled metadata rather than local identity.
    """
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    if value in {".", ".."} or value.startswith((".", "-")):
        return False
    if Path(value).is_absolute() or "/" in value or "\\" in value:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return all(char.isascii() and (char.isalnum() or char in "._-") for char in value)


def _detect_env(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (conda_env_name, relative_env_file).

    A repo may ship several env files (driver env + per-rule envs). We want the
    env that actually drives Snakemake, so: root environment.* wins; otherwise
    prefer an envs/* file that mentions snakemake or is named like the repo.
    """
    root = [path / "environment.yml", path / "environment.yaml"]
    for c in root:
        if c.exists():
            return _conda_env_name(c), str(c.relative_to(path))

    candidates = sorted(path.glob("envs/*.yml")) + sorted(path.glob("envs/*.yaml"))
    candidates = [c for c in candidates if c.exists()]
    if not candidates:
        return None, None

    def score(c: Path) -> tuple:
        return (
            _env_mentions_snakemake(c),  # driver env first
            path.name.lower() in c.stem.lower(),  # named like the repo
            c.stem.lower() in ("environment", "env"),  # generic env name
        )

    best = max(candidates, key=score)
    return _conda_env_name(best), str(best.relative_to(path))


def _discover_one(path: Path) -> Optional[Pipeline]:
    name = path.name
    if not _valid_pipeline_id(name):
        return None

    # Snakefile: prefer workflow/Snakefile
    snakefile = None
    for rel in ("workflow/Snakefile", "Snakefile"):
        if (path / rel).exists():
            snakefile = rel
            break

    # main configs
    configs = []
    for rel in ("config/config.yaml", "config/config.yml", "config.yaml", "config.yml"):
        if (path / rel).exists():
            configs.append(rel)
    project_local = sorted(
        str(p.relative_to(path)) for p in path.glob("project_local/*.y*ml")
    )
    test_configs = sorted(
        {
            str(c.relative_to(path))
            for pat in (
                ".test/*config*.y*ml",
                "config*.test.y*ml",
                "*test_config*.y*ml",
            )
            for c in path.glob(pat)
        }
    )

    # profiles
    profiles = []
    pdir = path / "profiles"
    if pdir.is_dir():
        profiles = sorted(p.name for p in pdir.iterdir() if p.is_dir())

    conda_env, env_file = _detect_env(path)

    readme = None
    for rel in ("README.md", "README.rst", "README.txt"):
        if (path / rel).exists():
            readme = rel
            break

    # kind + entrypoint
    if snakefile:
        kind = "snakemake"
        entrypoint = None
    elif (path / "run_pipeline.py").exists():
        kind, entrypoint = "python", "run_pipeline.py"
    elif (path / "pyproject.toml").exists():
        kind, entrypoint = "python", "pyproject.toml"
    else:
        shells = sorted(path.glob("*.sh"))
        if shells:
            kind, entrypoint = "shell", shells[0].name
        else:
            kind, entrypoint = "unknown", None

    if kind == "unknown" and not configs and not readme:
        return None  # not a pipeline repo

    return Pipeline(
        name=name,
        path=path,
        kind=kind,
        snakefile=snakefile,
        configs=configs,
        project_local=project_local,
        test_configs=test_configs,
        profiles=profiles,
        conda_env=conda_env,
        env_file=env_file,
        readme=readme,
        entrypoint=entrypoint,
    )


def discover() -> list[Pipeline]:
    try:
        root = settings.ROOT.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if not root.is_dir():
        return []

    out: list[Pipeline] = []
    seen: set[str] = set()
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        # ``Path.is_dir`` follows symlinks.  Check the directory entry first so
        # a symlink under ROOT can never register a pipeline outside ROOT.
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name in settings.EXCLUDE_DIRS or not _valid_pipeline_id(child.name):
            continue
        try:
            resolved = child.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.parent != root:
            continue
        p = _discover_one(resolved)
        if p and p.name not in seen:
            seen.add(p.name)
            out.append(p)
    return out


def get(name: str) -> Optional[Pipeline]:
    if not _valid_pipeline_id(name) or name in settings.EXCLUDE_DIRS:
        return None
    return next((pipeline for pipeline in discover() if pipeline.name == name), None)


# --- conda env resolution + command construction --------------------------
def _conda_exe() -> Optional[str]:
    found = shutil.which("mamba") or shutil.which("conda")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / "miniforge3" / "bin" / "mamba",
        home / "miniforge3" / "bin" / "conda",
        home / "mambaforge" / "bin" / "mamba",
        home / "miniconda3" / "bin" / "conda",
        Path("/opt/conda/bin/conda"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


_ENV_CACHE: dict = {"t": 0.0, "data": None}


def env_index(force: bool = False) -> dict[str, dict]:
    """Map env name -> {path, has_snakemake}.

    Env names can collide across conda roots (e.g. two `snakemake` envs); when
    they do we keep the one that actually contains a snakemake binary, and we
    later launch with `-p <path>` so name ambiguity can't bite us. Cached 30s.
    """
    now = time.time()
    if not force and _ENV_CACHE["data"] is not None and now - _ENV_CACHE["t"] < 30:
        return _ENV_CACHE["data"]
    exe = _conda_exe()
    index: dict[str, dict] = {}
    if exe:
        try:
            out = subprocess.run(
                [exe, "env", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            for path in json.loads(out.stdout).get("envs", []):
                if not path:
                    continue
                name = os.path.basename(path)
                if not name or "/" in name:
                    continue
                has = os.path.exists(os.path.join(path, "bin", "snakemake"))
                if name not in index or (has and not index[name]["has_snakemake"]):
                    index[name] = {"path": path, "has_snakemake": has}
        except Exception:
            pass
    _ENV_CACHE.update(t=now, data=index)
    return index


def real_envs() -> list[str]:
    return sorted(env_index().keys())


def snakemake_envs() -> list[str]:
    """Env names that actually contain a snakemake binary."""
    return sorted(n for n, i in env_index().items() if i["has_snakemake"])


def resolve_env(p: Pipeline, available: Optional[list[str]] = None) -> Optional[str]:
    """Best-guess conda env to DRIVE Snakemake — only envs that really have it."""
    idx = env_index()
    sm = [n for n, i in idx.items() if i["has_snakemake"]]
    if not sm:
        return p.conda_env
    declared = (p.conda_env or "").lower()
    lname = p.name.lower()
    local_name = p.path.name.lower()
    # 1) declared name actually exists AND has snakemake
    for n in sm:
        if n.lower() == declared and declared:
            return n
    # 2) snakemake env named like the pipeline
    for n in sm:
        nl = n.lower()
        if (lname and (lname in nl or nl in lname)) or (
            local_name and (local_name in nl or nl in local_name)
        ):
            return n
    # 3) family / generic driver envs, in priority order
    for cand in ("oracle-smk", "snakemake", "smk"):
        if cand in sm:
            return cand
    return sm[0]


def _conda_run_prefix(conda_exe: str, env: str) -> list[str]:
    """Launch prefix for running inside `env`.

    Uses `-p <path>` when we know the env's path (avoids duplicate-name
    ambiguity). mamba 2.x streams natively and rejects --no-capture-output;
    conda needs it.
    """
    idx = env_index()
    target = ["-p", idx[env]["path"]] if env in idx else ["-n", env]
    base = Path(conda_exe).name
    if base.startswith(("mamba", "micromamba")):
        return [conda_exe, "run", *target]
    return [conda_exe, "run", "--no-capture-output", *target]


def build_snakemake_argv(
    p: Pipeline,
    *,
    target: str = "",
    configfile: str = "",
    profile: str = "",
    cores: int = 8,
    dryrun: bool = True,
    use_conda: bool = True,
    extra: str = "",
    env: str = "",
    resolve_default_env: bool = True,
) -> tuple[list[str], str]:
    """Return (argv, human_command). argv is what we exec in the PTY."""
    import shlex

    snake: list[str] = ["snakemake"]
    if p.snakefile:
        snake += ["-s", p.snakefile]
    snake += ["--cores", str(cores)]
    if dryrun:
        snake += ["-n", "-p"]
    if use_conda:
        snake += ["--use-conda"]
    if configfile:
        snake += ["--configfile", configfile]
    if profile:
        snake += ["--profile", f"profiles/{profile}"]
    if extra:
        snake += shlex.split(extra)
    if target:
        snake += [target]

    # wrap in conda/mamba run so snakemake resolves inside the chosen env
    conda = _conda_exe()
    chosen = env or resolve_env(p) if resolve_default_env else env
    if chosen and conda:
        argv = [*_conda_run_prefix(conda, chosen), *snake]
    else:
        argv = snake

    human = " ".join(shlex.quote(a) for a in argv)
    return argv, human


GRAPH_MODES = ("rulegraph", "dag", "filegraph")


def build_snakemake_graph_argv(
    p: Pipeline,
    *,
    mode: str = "rulegraph",
    configfile: str = "",
    env: str = "",
) -> tuple[list[str], str]:
    """argv for `snakemake --<mode>` which emits a Graphviz DOT graph on stdout.

    rulegraph = compact rule dependency graph (structural; works without inputs).
    dag       = per-job graph (needs inputs to resolve; can fail like a dry-run).
    filegraph = file-level graph.
    """
    import shlex

    snake: list[str] = ["snakemake"]
    if p.snakefile:
        snake += ["-s", p.snakefile]
    snake += [f"--{mode}"]
    if configfile:
        snake += ["--configfile", configfile]

    conda = _conda_exe()
    chosen = env or resolve_env(p)
    if chosen and conda:
        argv = [*_conda_run_prefix(conda, chosen), *snake]
    else:
        argv = snake
    return argv, " ".join(shlex.quote(a) for a in argv)
