"""Configure existing App Service using its existing secrets; never print keys."""

import json
import secrets
import subprocess
import sys


APP_NAME = "sk1127ihb50609"
STORAGE_ACCOUNT = "ih50609"


def azure(*arguments):
    result = subprocess.run(["az", *arguments, "--only-show-errors", "--output", "json"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Azure設定を確認できません。連携IDのApp Service設定権限を確認してください。")
    return json.loads(result.stdout or "null")


def main():
    matches = azure("webapp", "list", "--query", f"[?name=='{APP_NAME}']")
    if len(matches) != 1:
        raise RuntimeError("公開先App Serviceを一意に確認できません。連携IDの権限を確認してください。")
    app = matches[0]
    group = app["resourceGroup"]
    arguments = ("--resource-group", group, "--name", APP_NAME)
    settings = {entry["name"]: entry.get("value", "") for entry in azure("webapp", "config", "appsettings", "list", *arguments)}
    updates = {
        "STORAGE_BACKEND": "azure", "COOKIE_SECURE": "true",
        "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
    }
    if len(settings.get("SECRET_KEY", "")) < 32:
        updates["SECRET_KEY"] = secrets.token_urlsafe(48)
    connection = settings.get("AZURE_STORAGE_CONNECTION_STRING", "")
    account_url = settings.get("AZURE_STORAGE_ACCOUNT_URL", "")
    if not connection and not account_url:
        candidates = [value for value in settings.values() if f"AccountName={STORAGE_ACCOUNT};" in value and "AccountKey=" in value]
        if not candidates:
            existing = azure("webapp", "config", "connection-string", "list", *arguments)
            candidates = [entry.get("value", "") for entry in existing if f"AccountName={STORAGE_ACCOUNT};" in entry.get("value", "") and "AccountKey=" in entry.get("value", "")]
        if candidates:
            updates["AZURE_STORAGE_CONNECTION_STRING"] = candidates[0]
        else:
            raise RuntimeError("App Serviceの環境変数AZURE_STORAGE_CONNECTION_STRINGにBlob接続文字列を設定してください。値はGitHubやチャットに保存しないでください。")
    azure("webapp", "config", "appsettings", "set", *arguments, "--settings", *[f"{key}={value}" for key, value in updates.items()])
    print("App Service設定完了。接続情報・秘密キーは非表示です。")
    print("公開URL: https://" + app["defaultHostName"])


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
