# OFAC Automation

Windows desktop app for screening names on the live OFAC Sanctions List Search site and printing each result page to PDF.

## Run a check

Paste names or import a TXT or CSV file, then click **Run Check**. Click **Cancel Check** to stop and **Resume Check** to continue with the remaining names. Completed results and PDFs stay available until you start another check or close the app.

The app checks one name at a time and waits for a fresh result before printing it. It retries temporary failures and observes any cooldown requested by the site. If a run pauses, **Resume Check** starts with the next unfinished name.

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
