from __future__ import annotations

import ctypes
import os
import queue
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable
import tkinter as tk

import customtkinter as ctk

from ofac_checker import (
    OfacResult,
    default_pdf_name,
    load_names_from_file,
    parse_names,
    search_ofac_names,
)


COLORS = {
    "page": "#f3f7f8",
    "ink": "#17242a",
    "muted": "#647178",
    "line": "#d8e3e6",
    "surface": "#ffffff",
    "surface_alt": "#f7fafb",
    "accent": "#007999",
    "accent_hover": "#00647f",
    "accent_soft": "#e7f6fa",
    "success": "#28735b",
    "danger": "#a63d36",
    "warning": "#8a6424",
    "quiet_button": "#edf4f6",
    "quiet_hover": "#dfeaed",
}

FONT_BODY = ("Aptos", 14)
FONT_SMALL = ("Aptos", 12)
FONT_TITLE = ("Aptos Display", 30, "bold")
FONT_SECTION = ("Aptos Display", 18, "bold")
APP_USER_MODEL_ID = "Internal.OFACAutomation"
WINDOW_ICON_NAME = "OFAC.ico"


class ResultCard(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTkBaseClass,
        item_id: str,
        result: OfacResult,
        on_save: Callable[[str], None],
        on_open: Callable[[str], None],
    ) -> None:
        super().__init__(
            master,
            fg_color=COLORS["surface"],
            corner_radius=16,
            border_width=1,
            border_color=COLORS["line"],
        )

        self.item_id = item_id
        self.result = result
        self.on_save = on_save
        self.on_open = on_open

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=16, pady=12)
        left.grid_columnconfigure(0, weight=1)

        self.name_label = ctk.CTkLabel(
            left,
            text=result.company,
            font=("Aptos", 15, "bold"),
            text_color=COLORS["ink"],
            anchor="w",
        )
        self.name_label.grid(row=0, column=0, sticky="ew")

        detail = result.result_text or result.error or "No result text"
        self.detail_label = ctk.CTkLabel(
            left,
            text=detail,
            font=FONT_SMALL,
            text_color=COLORS["muted"],
            anchor="w",
        )
        self.detail_label.grid(row=1, column=0, sticky="ew", pady=(3, 0))

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=(0, 14), pady=12)

        self.status_label = ctk.CTkLabel(
            right,
            text=result.status,
            font=("Aptos", 11, "bold"),
            text_color=self.status_color(),
            fg_color="transparent",
            height=18,
        )
        self.status_label.grid(row=0, column=0, columnspan=2, sticky="e", pady=(0, 8))

        can_use_pdf = bool(result.pdf_path and result.pdf_path.exists())
        button_state = "normal" if can_use_pdf else "disabled"

        self.save_button = ctk.CTkButton(
            right,
            text="Download",
            width=98,
            height=32,
            corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            command=lambda: self.on_save(self.item_id),
            state=button_state,
        )
        self.save_button.grid(row=1, column=0, padx=(0, 6))

        self.open_button = ctk.CTkButton(
            right,
            text="Open",
            width=70,
            height=32,
            corner_radius=10,
            fg_color=COLORS["quiet_button"],
            hover_color=COLORS["quiet_hover"],
            text_color=COLORS["ink"],
            command=lambda: self.on_open(self.item_id),
            state=button_state,
        )
        self.open_button.grid(row=1, column=1)

    def status_color(self) -> str:
        if self.result.status == "Clean":
            return COLORS["success"]
        if self.result.status == "Review needed":
            return COLORS["danger"]
        return COLORS["warning"]


class OfacAutomationApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("OFAC Automation")
        self.set_window_icon()
        self.geometry("1180x760")
        self.minsize(1060, 680)

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False

        self.results_by_id: dict[str, OfacResult] = {}
        self.result_cards: dict[str, ResultCard] = {}
        self.result_counter = 0
        self.last_save_dir = get_default_output_dir()
        self.report_root = get_internal_report_dir()
        self.current_report_dir: Path | None = None

        cleanup_report_root(self.report_root)
        self.status_var = tk.StringVar(value="Ready")
        self.summary_var = tk.StringVar(value="0 checked")
        self.name_count_var = tk.StringVar(value="0 names")

        self.configure(fg_color=COLORS["page"])
        self.create_widgets()
        self.after(100, self.process_messages)
        self.protocol("WM_DELETE_WINDOW", self.handle_close)

    def set_window_icon(self) -> None:
        icon_path = get_resource_path(WINDOW_ICON_NAME)
        if not icon_path.exists():
            return

        try:
            self.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass

    def create_widgets(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 12))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="OFAC Automation",
            font=FONT_TITLE,
            text_color=COLORS["ink"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text="Screen names and save PDF records.",
            font=FONT_BODY,
            text_color=COLORS["muted"],
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.run_button = ctk.CTkButton(
            header,
            text="Run Check",
            width=150,
            height=44,
            corner_radius=16,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            font=("Aptos", 14, "bold"),
            command=self.start_search,
        )
        self.run_button.grid(row=0, column=1, rowspan=2, sticky="e")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 18))
        content.grid_columnconfigure(0, weight=2, minsize=350)
        content.grid_columnconfigure(1, weight=5)
        content.grid_rowconfigure(0, weight=1)

        self.create_input_panel(content)
        self.create_results_panel(content)
        self.create_footer()

    def create_input_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            corner_radius=24,
            border_width=1,
            border_color=COLORS["line"],
        )
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        title_row = ctk.CTkFrame(panel, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 0))
        title_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            title_row,
            text="Names",
            font=FONT_SECTION,
            text_color=COLORS["ink"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_row,
            textvariable=self.name_count_var,
            font=("Aptos", 12, "bold"),
            text_color=COLORS["accent"],
            fg_color=COLORS["accent_soft"],
            corner_radius=14,
            width=82,
            height=28,
        ).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(
            panel,
            text="Paste names here, one per line.",
            font=FONT_SMALL,
            text_color=COLORS["muted"],
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 12))

        self.names_text = ctk.CTkTextbox(
            panel,
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["ink"],
            border_color=COLORS["line"],
            border_width=1,
            corner_radius=18,
            font=FONT_BODY,
            wrap="word",
        )
        self.names_text.grid(row=2, column=0, sticky="nsew", padx=18)
        self.names_text.bind("<KeyRelease>", lambda _event: self.refresh_name_count())
        self.names_text.bind("<<Paste>>", lambda _event: self.after(1, self.refresh_name_count))

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=18, pady=18)
        actions.grid_columnconfigure(2, weight=1)

        self.import_button = ctk.CTkButton(
            actions,
            text="Import",
            width=96,
            height=36,
            corner_radius=13,
            fg_color=COLORS["quiet_button"],
            hover_color=COLORS["quiet_hover"],
            text_color=COLORS["ink"],
            command=self.import_file,
        )
        self.import_button.grid(row=0, column=0, padx=(0, 8))

        self.clear_names_button = ctk.CTkButton(
            actions,
            text="Clear",
            width=82,
            height=36,
            corner_radius=13,
            fg_color=COLORS["surface_alt"],
            hover_color=COLORS["quiet_hover"],
            text_color=COLORS["muted"],
            command=self.clear_names,
        )
        self.clear_names_button.grid(row=0, column=1)

    def create_results_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            corner_radius=24,
            border_width=1,
            border_color=COLORS["line"],
        )
        panel.grid(row=0, column=1, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        title_row = ctk.CTkFrame(panel, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 0))
        title_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            title_row,
            text="Results",
            font=FONT_SECTION,
            text_color=COLORS["ink"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_row,
            textvariable=self.summary_var,
            font=("Aptos", 12, "bold"),
            text_color=COLORS["muted"],
        ).grid(row=0, column=1, sticky="e")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", padx=18, pady=(12, 10))
        actions.grid_columnconfigure(1, weight=1)

        self.save_all_button = ctk.CTkButton(
            actions,
            text="Download All",
            width=122,
            height=34,
            corner_radius=12,
            fg_color=COLORS["quiet_button"],
            hover_color=COLORS["quiet_hover"],
            text_color=COLORS["ink"],
            command=self.save_all_pdfs,
            state="disabled",
        )
        self.save_all_button.grid(row=0, column=0, padx=(0, 8))

        self.results_list = ctk.CTkScrollableFrame(
            panel,
            fg_color=COLORS["surface_alt"],
            corner_radius=20,
            scrollbar_button_color="#c9d9de",
            scrollbar_button_hover_color="#adc3ca",
        )
        self.results_list.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 18))
        self.results_list.grid_columnconfigure(0, weight=1)

        self.empty_state = ctk.CTkLabel(
            self.results_list,
            text="Run a check to see results here.",
            font=FONT_BODY,
            text_color=COLORS["muted"],
        )
        self.empty_state.grid(row=0, column=0, pady=36)

    def create_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 18))
        footer.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(
            footer,
            height=10,
            corner_radius=8,
            progress_color=COLORS["accent"],
            fg_color="#d9e5e8",
        )
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 14))
        self.progress.set(0)

        ctk.CTkLabel(
            footer,
            textvariable=self.status_var,
            font=FONT_SMALL,
            text_color=COLORS["muted"],
        ).grid(row=0, column=1, sticky="e")

    def import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Import names",
            filetypes=[
                ("Supported files", "*.txt *.csv"),
                ("Text files", "*.txt"),
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        try:
            names = load_names_from_file(Path(path))
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))
            return

        if not names:
            messagebox.showwarning("No names found", "That file did not contain any usable names.")
            return

        current = parse_names(self.names_text.get("1.0", "end"))
        current_keys = {name.casefold() for name in current}
        merged = current + [name for name in names if name.casefold() not in current_keys]
        self.set_names(merged)
        self.status_var.set(f"Imported {len(names)} names")

    def clear_names(self) -> None:
        self.names_text.delete("1.0", "end")
        self.refresh_name_count()
        self.status_var.set("Ready")

    def set_names(self, names: list[str]) -> None:
        self.names_text.delete("1.0", "end")
        self.names_text.insert("1.0", "\n".join(names))
        self.refresh_name_count()

    def refresh_name_count(self) -> None:
        count = len(parse_names(self.names_text.get("1.0", "end")))
        label = "name" if count == 1 else "names"
        self.name_count_var.set(f"{count} {label}")

    def start_search(self) -> None:
        if self.running:
            return

        companies = parse_names(self.names_text.get("1.0", "end"))
        if not companies:
            messagebox.showwarning("No names", "Add at least one name.")
            return

        self.clear_results()
        cleanup_report_root(self.report_root)
        self.current_report_dir = self.report_root / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.current_report_dir.mkdir(parents=True, exist_ok=True)

        self.progress_total = len(companies)
        self.progress_done = 0
        self.progress.set(0)
        self.status_var.set("Searching OFAC...")
        self.set_running(True)

        self.worker = threading.Thread(
            target=self.search_worker,
            args=(companies, self.current_report_dir),
            daemon=True,
        )
        self.worker.start()

    def search_worker(self, companies: list[str], report_dir: Path) -> None:
        try:
            search_ofac_names(
                companies,
                progress=lambda message: self.messages.put(("status", message)),
                result_callback=lambda result: self.messages.put(("result", result)),
                report_dir=report_dir,
            )
            self.messages.put(("search_done", None))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def process_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break

            if kind == "status":
                self.status_var.set(str(payload))
            elif kind == "result":
                self.add_result(payload)
                self.increment_progress()
            elif kind == "search_done":
                self.set_running(False)
                self.progress.set(1)
                self.status_var.set("Complete")
                self.refresh_summary()
            elif kind == "error":
                self.set_running(False)
                self.status_var.set("Stopped")
                messagebox.showerror("OFAC Automation", str(payload))

        self.after(100, self.process_messages)

    def add_result(self, result: OfacResult) -> None:
        if self.empty_state.winfo_exists():
            self.empty_state.grid_forget()

        item_id = f"result-{self.result_counter}"
        self.result_counter += 1

        card = ResultCard(
            self.results_list,
            item_id,
            result,
            on_save=self.save_result_pdf,
            on_open=self.open_result_pdf,
        )
        card.grid(row=len(self.result_cards), column=0, sticky="ew", padx=10, pady=(10, 0))

        self.results_by_id[item_id] = result
        self.result_cards[item_id] = card
        self.refresh_summary()
        self.refresh_actions()

    def save_result_pdf(self, item_id: str) -> None:
        result = self.results_by_id[item_id]
        if not result.pdf_path or not result.pdf_path.exists():
            messagebox.showwarning("No PDF", "Run the check again to recreate this PDF.")
            return

        initial_dir = self.last_save_dir if self.last_save_dir.exists() else get_default_output_dir()
        path = filedialog.asksaveasfilename(
            title="Save OFAC PDF",
            initialdir=str(initial_dir),
            initialfile=default_pdf_name(result.company),
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")],
        )
        if not path:
            return

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(result.pdf_path, destination)
        self.last_save_dir = destination.parent
        self.status_var.set(f"Saved {destination.name}")

    def save_all_pdfs(self) -> None:
        saveable = [result for result in self.results_by_id.values() if result.pdf_path and result.pdf_path.exists()]
        if not saveable:
            messagebox.showwarning("No PDFs", "Run a check before saving PDFs.")
            return

        folder = filedialog.askdirectory(
            title="Choose PDF folder",
            initialdir=str(self.last_save_dir if self.last_save_dir.exists() else get_default_output_dir()),
        )
        if not folder:
            return

        folder_path = Path(folder)
        folder_path.mkdir(parents=True, exist_ok=True)

        for result in saveable:
            shutil.copy2(result.pdf_path, folder_path / default_pdf_name(result.company))

        self.last_save_dir = folder_path
        self.status_var.set(f"Saved {len(saveable)} PDFs")

    def open_result_pdf(self, item_id: str) -> None:
        result = self.results_by_id[item_id]
        if not result.pdf_path or not result.pdf_path.exists():
            return
        os.startfile(result.pdf_path)

    def clear_results(self) -> None:
        for card in self.result_cards.values():
            card.destroy()

        self.results_by_id.clear()
        self.result_cards.clear()
        self.result_counter = 0

        self.empty_state.grid(row=0, column=0, pady=36)
        self.progress.set(0)
        self.refresh_summary()
        self.refresh_actions()

    def refresh_summary(self) -> None:
        results = list(self.results_by_id.values())
        review = sum(1 for result in results if result.status == "Review needed")
        errors = sum(1 for result in results if result.error)

        if not results:
            self.summary_var.set("0 checked")
            return

        self.summary_var.set(f"{len(results)} checked   {review} review   {errors} errors")

    def refresh_actions(self) -> None:
        has_pdf = any(result.pdf_path and result.pdf_path.exists() for result in self.results_by_id.values())
        state = "normal" if has_pdf and not self.running else "disabled"
        self.save_all_button.configure(state=state)

    def increment_progress(self) -> None:
        self.progress_done += 1
        if self.progress_total:
            self.progress.set(min(self.progress_done / self.progress_total, 1))

    def set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"

        self.run_button.configure(state=state)
        self.import_button.configure(state=state)
        self.clear_names_button.configure(state=state)
        self.refresh_actions()

    def handle_close(self) -> None:
        if self.running:
            messagebox.showinfo("Still running", "Please wait for the current check to finish.")
            return
        cleanup_report_root(self.report_root)
        self.destroy()


def get_default_output_dir() -> Path:
    documents = Path.home() / "Documents"
    if documents.exists():
        return documents / "OFAC Automation Output"
    return get_app_dir() / "Output"


def get_internal_report_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "OFAC Automation" / "Reports"
    return get_app_dir() / "Reports"


def cleanup_report_root(report_root: Path) -> None:
    if not report_root.exists():
        return

    for child in report_root.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            pass


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        bundle_dir = Path(getattr(sys, "_MEIPASS", get_app_dir()))
        bundled_path = bundle_dir / name
        if bundled_path.exists():
            return bundled_path
        return get_app_dir() / name

    return Path(__file__).resolve().parent / name


def enable_high_dpi() -> None:
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def main() -> None:
    enable_high_dpi()
    set_windows_app_user_model_id()
    ctk.set_appearance_mode("light")
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)
    app = OfacAutomationApp()
    app.mainloop()


if __name__ == "__main__":
    main()
