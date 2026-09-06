from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_setup_installs_pytorch_from_official_cpu_index() -> None:
    setup = (PROJECT_ROOT / "setup.ps1").read_text(encoding="utf-8")
    torch_reqs = (PROJECT_ROOT / "backend" / "requirements-torch-cpu.txt").read_text(encoding="utf-8")
    assert "requirements-torch-cpu.txt" in setup
    assert "torch==2.14.0" in torch_reqs
    assert "torchvision==0.29.0" in torch_reqs
    assert "https://download.pytorch.org/whl/cpu" in torch_reqs
    assert "CUDA packages are intentionally forbidden" in setup


def test_requirements_cannot_override_cpu_only_pytorch_install() -> None:
    requirements = (PROJECT_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    package_names = {
        line.split("==", 1)[0].split("[", 1)[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "torch" not in package_names
    assert "torchvision" not in package_names


def test_project_remains_locked_to_python_312() -> None:
    assert (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    setup = (PROJECT_ROOT / "setup.ps1").read_text(encoding="utf-8")
    assert "py -3.12" in setup
    assert "sys.version_info[:2] == (3,12)" in setup


def test_phase6_embedding_dependency_is_pinned_and_cpu_only_by_policy() -> None:
    requirements = (PROJECT_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert "sentence-transformers==5.6.0" in requirements
    assert "onnxruntime-gpu" not in requirements.casefold()
    env = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DEDUP_EMBEDDING_MODEL=intfloat/multilingual-e5-small" in env
    assert "DEDUP_EMBEDDING_REVISION=fd1525a9fd15316a2d503bf26ab031a61d056e98" in env
