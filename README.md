# OFAC Automation

Windows desktop app for screening names against the public OFAC Sanctions Search site and downloading PDF records from each result row.

## Run From Source

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
.\venv\Scripts\python.exe app.py
```

## Build Installer

Install Inno Setup 6, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The installer is created at:

```text
installer\OFAC-Automation-Setup.exe
```
