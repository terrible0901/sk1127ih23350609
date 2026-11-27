"""Check the actual running app and print sanitized startup logs on failure."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from configure_azure import APP_NAME, azure


def main():
    matches = azure("webapp", "list", "--query", f"[?name=='{APP_NAME}']")
    app = matches[0]
    args = ("--resource-group", app["resourceGroup"], "--name", APP_NAME)
    base = "https://" + app["defaultHostName"]
    print("Verifying " + base, flush=True)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=20) as response:
                assert json.load(response) == {"status": "ok"}
            with urllib.request.urlopen(base + "/", timeout=20) as response:
                assert "放課後ノート" in response.read().decode("utf-8")
            print("Live Flask health and Blob-backed home page: OK", flush=True)
            return
        except (OSError, ValueError, AssertionError):
            print(f"App not ready (attempt {attempt + 1}/3).", flush=True)
            if attempt < 2:
                time.sleep(10)

    config = azure("webapp", "config", "show", *args)
    print("Runtime: " + str(config.get("linuxFxVersion")), flush=True)
    print("Startup: " + str(config.get("appCommandLine")), flush=True)
    settings = azure("webapp", "config", "appsettings", "list", *args)
    hidden = [entry.get("value", "") for entry in settings if len(entry.get("value", "") or "") > 5]
    try:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "logs.zip"
            result = subprocess.run([
                "az", "webapp", "log", "download", *args, "--log-file", str(destination), "--only-show-errors",
            ], capture_output=True, text=True, timeout=90)
            if result.returncode or not destination.exists():
                print("Startup log download unavailable. Check App Service > Log stream.", flush=True)
            else:
                with zipfile.ZipFile(destination) as archive:
                    candidates = [entry for entry in archive.infolist() if entry.filename.endswith((".log", ".txt")) and "deployments" not in entry.filename.lower()]
                    for entry in sorted(candidates, key=lambda item: item.filename)[-6:]:
                        with archive.open(entry) as file:
                            text = file.read(2_000_000).decode("utf-8", errors="replace")
                        for value in sorted(hidden, key=len, reverse=True):
                            text = text.replace(value, "[REDACTED]")
                        print("--- " + entry.filename + " ---", flush=True)
                        print("\n".join(text.splitlines()[-60:]), flush=True)
    except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile):
        print("Could not download startup logs.", flush=True)
    raise RuntimeError("Live app verification failed; deployment is not complete.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
