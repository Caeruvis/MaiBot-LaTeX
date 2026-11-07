"""用系统隔离运行本机 TeX Live 与 Poppler，所有输入和临时文件限于当前任务。"""

import asyncio
import os
import re
import shutil
import signal
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

_PROFILE = Path(__file__).resolve().parents[1] / "texlive/macos.sb"
_MAC_PATH = "/Library/TeX/texbin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_LIMITS = 'ulimit -c 0 && ulimit -f 16384 && ulimit -t 10 && exec "$@"'
_PAGE_SIZE = re.compile(rb"^Page size:\s*([\d.]+) x ([\d.]+) pts", re.MULTILINE)


def _executable(name: str, search_path: str) -> Path:
    value = shutil.which(name, path=search_path)
    if not value:
        raise FileNotFoundError(name)
    return Path(value).resolve(strict=True)


class _Job:
    def __init__(self, work: Path):
        self.work = work
        if sys.platform == "darwin":
            search_path = _MAC_PATH
            self.xetex = _executable("xelatex", search_path)
            self.texroot = self.xetex.parents[2]
            if (
                self.texroot.parent != Path("/usr/local/texlive")
                or not (self.texroot / "texmf-dist").is_dir()
            ):
                raise FileNotFoundError("MacTeX installation")
            self.driver = _executable("xdvipdfmx", str(self.xetex.parent))
            work_path = str(work)
        elif sys.platform == "linux":
            search_path = "/usr/bin:/bin"
            self.xetex = _executable("xelatex", search_path)
            self.driver = _executable("xdvipdfmx", search_path)
            self.bwrap = _executable("bwrap", search_path)
            work_path = "/work"
        else:
            raise OSError("No supported TeX isolation")
        self.pdfinfo = _executable("pdfinfo", search_path)
        self.pdftoppm = _executable("pdftoppm", search_path)
        self.env = {
            "LC_ALL": "C",
            "TZ": "UTC0",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": work_path,
            "XDG_CACHE_HOME": work_path + "/cache",
            "TEXMFHOME": work_path + "/texmf-home",
            "TEXMFVAR": work_path + "/texmf-var",
            "TEXMFCONFIG": work_path + "/texmf-config",
            "openin_any": "p",
            "openout_any": "p",
        }

    def _sandbox(self, program: Path) -> list[str]:
        if sys.platform == "darwin":
            return [
                "/usr/bin/sandbox-exec",
                "-f",
                str(_PROFILE),
                "-D",
                f"EXECUTABLE={program}",
                "-D",
                f"WORK={self.work}",
                "-D",
                f"TEXROOT={self.texroot}",
                str(program),
            ]
        return [
            str(self.bwrap),
            "--unshare-all",
            "--die-with-parent",
            "--ro-bind",
            "/usr",
            "/usr",
            "--tmpfs",
            "/usr/local",
            "--ro-bind-try",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--ro-bind-try",
            "/etc/ld.so.cache",
            "/etc/ld.so.cache",
            "--ro-bind-try",
            "/etc/fonts",
            "/etc/fonts",
            "--ro-bind-try",
            "/etc/texmf",
            "/etc/texmf",
            "--ro-bind-try",
            "/var/lib/texmf",
            "/var/lib/texmf",
            "--dev",
            "/dev",
            "--bind",
            str(self.work),
            "/work",
            "--chdir",
            "/work",
            str(program),
        ]

    async def run(self, program: Path, *args: str, limit: int = 0) -> bytes | None:
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            _LIMITS,
            "texlive-limits",
            *self._sandbox(program),
            *args,
            cwd=self.work,
            env=self.env,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if limit else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output = b""
            if limit:
                try:
                    await process.stdout.readexactly(limit + 1)
                    return None
                except asyncio.IncompleteReadError as exc:
                    output = exc.partial
            return output if await process.wait() == 0 else None
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                if process.stdout is not None:
                    while await process.stdout.read(65536):
                        pass
                await process.wait()


async def compile_png(document: bytes, dpi: int, width: int, height: int, limit: int) -> bytes:
    with TemporaryDirectory(prefix="maibot-texlive-") as temp:
        work = Path(temp).resolve()
        job = _Job(work)
        (work / "answer.tex").write_bytes(document)
        if (
            await job.run(
                job.xetex,
                "-fmt=xelatex",
                "-progname=xelatex",
                "-no-pdf",
                "-no-shell-escape",
                "-halt-on-error",
                "-interaction=batchmode",
                "answer.tex",
            )
            is None
        ):
            return b""
        log = work / "answer.log"
        if (
            log.is_symlink()
            or not log.is_file()
            or re.search(rb"(?m)^Overfull .*box", log.read_bytes())
        ):
            return b""
        if await job.run(job.driver, "-q", "-o", "answer.pdf", "answer.xdv") is None:
            return b""
        info = await job.run(job.pdfinfo, "answer.pdf", limit=16384)
        if not info or not re.search(rb"(?m)^Pages:[ \t]+1[ \t]*$", info):
            return b""
        size = _PAGE_SIZE.search(info)
        if not size:
            return b""
        page_width, page_height = map(float, size.groups())
        if not (0 < page_width * dpi <= width * 72 and 0 < page_height * dpi <= height * 72):
            return b""
        return (
            await job.run(
                job.pdftoppm,
                "-png",
                "-singlefile",
                "-r",
                str(dpi),
                "answer.pdf",
                limit=limit,
            )
            or b""
        )
