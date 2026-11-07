"""构建本地安装包并逐文件核验压缩包内容。"""

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDED_DIRS = ("latex_engine", "vendor", "docs", "tests", "tools")
INCLUDED_FILES = (
    "plugin.py",
    "_manifest.json",
    "README.md",
    "config.example.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "pytest.ini",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)


def main():
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    paths = [ROOT / name for name in INCLUDED_FILES]
    for folder in INCLUDED_DIRS:
        paths.extend(
            p
            for p in (ROOT / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )
    paths = sorted(paths)
    archive = output / "maibot-latex-0.1.0.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in paths:
            z.write(path, "caeruvis_latex/" + path.relative_to(ROOT).as_posix())
    report = {
        "archive": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "files": {},
        "verified": True,
    }
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            data = z.read("caeruvis_latex/" + relative)
            assert data == path.read_bytes()
            report["files"][relative] = hashlib.sha256(data).hexdigest()
    (output / "package-verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    print(archive)
    print(f"verified {len(paths)} files; sha256={report['sha256']}")


if __name__ == "__main__":
    main()
