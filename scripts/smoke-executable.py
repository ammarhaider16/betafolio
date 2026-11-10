"""Exercise a native executable without network calls or user configuration."""
import argparse
import json
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--archive", type=Path, help="Validate and extract the ZIP before exercising its executable")
    args = parser.parse_args()
    executable = args.executable.resolve()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PORTFOLIO_", "LANGCHAIN_", "LANGSMITH_"))
           and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LITELLM_API_KEY", "PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="Betafolio packaged smoke ") as directory:
        if args.archive:
            archive = args.archive.resolve()
            with zipfile.ZipFile(archive) as zipped:
                names = zipped.namelist()
                forbidden = {".tools", ".git", "tests", "deployment-secrets", "settings.json", "last_run.html"}
                assert not any(forbidden.intersection(Path(name).parts) or name.endswith((".sqlite3", ".env"))
                               for name in names), "Unexpected development or private files in ZIP"
            extracted = Path(directory) / "extracted"
            extracted.mkdir()
            if os.name == "nt":
                with zipfile.ZipFile(archive) as zipped:
                    zipped.extractall(extracted)
                executable = extracted / "Betafolio" / "Betafolio.exe"
            else:
                subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
                executable = extracted / "betafolio" / "betafolio"
        result = subprocess.run([str(executable), "--data-dir", directory, "analyze", "--demo", "--offline"],
                                capture_output=True, text=True, timeout=90, env=env)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == "success", report
        assert Decimal(report["metrics"]["known_value"]) == 42890, report["metrics"]
        print("Packaged standalone offline analysis passed. The web deployment requires Postgres and a worker.")



if __name__ == "__main__":
    main()
