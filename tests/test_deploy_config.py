from scripts import configure_azure


def test_deploy_reuses_existing_secret_and_connection_without_printing_values(monkeypatch, capsys):
    secret = "existing-session-secret-with-more-than-32-characters"
    connection = "DefaultEndpointsProtocol=https;AccountName=ih50609;AccountKey=test-placeholder;"
    recorded = []

    def azure(*args):
        recorded.append(args)
        if args[:2] == ("webapp", "list"):
            return [{"resourceGroup": "test-group", "defaultHostName": "test.azurewebsites.net"}]
        if args[:4] == ("webapp", "config", "appsettings", "list"):
            return [{"name": "SECRET_KEY", "value": secret}, {"name": "MY_BLOB_CONNECTION", "value": connection}]
        if args[:4] == ("webapp", "config", "appsettings", "set"):
            return []
        raise AssertionError(args)

    monkeypatch.setattr(configure_azure, "azure", azure)
    configure_azure.main()
    updates = recorded[-1]
    assert "AZURE_STORAGE_CONNECTION_STRING=" + connection in updates
    assert not any(arg.startswith("SECRET_KEY=") for arg in updates)
    output = capsys.readouterr().out
    assert secret not in output and connection not in output
    assert "https://test.azurewebsites.net" in output


def test_deploy_initializes_secret_once_and_uses_existing_connection(monkeypatch):
    calls = []

    def azure(*args):
        calls.append(args)
        if args[:2] == ("webapp", "list"):
            return [{"resourceGroup": "test-group", "defaultHostName": "test.azurewebsites.net"}]
        if args[:4] == ("webapp", "config", "appsettings", "list"):
            return [{"name": "AZURE_STORAGE_CONNECTION_STRING", "value": "already-configured"}]
        return []

    monkeypatch.setattr(configure_azure, "azure", azure)
    configure_azure.main()
    secrets = [arg.split("=", 1)[1] for arg in calls[-1] if arg.startswith("SECRET_KEY=")]
    assert len(secrets) == 1 and len(secrets[0]) >= 32

