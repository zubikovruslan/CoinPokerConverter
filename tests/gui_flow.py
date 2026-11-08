"""Exercise the actual Tk window and worker with synthetic data only."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.gui import Application
from tests.fixtures import regular, splash


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("gui_flow_result")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cpc_gui_") as temp:
        base = Path(temp)
        source = base / "Тест раздач.txt"
        source.write_text(regular()+"\n\n"+splash(), "utf-8")
        app = Application(data_dir=base/"settings")
        app.hero.set("TestPlayer")
        app.destination.set(str(base/"Результаты теста"))
        app.add_paths([source])
        app.root.update()
        def run():
            app.start_button.invoke()
            deadline = time.monotonic()+20
            while app.busy and time.monotonic() < deadline:
                app.root.update()
                time.sleep(.01)
            app.root.update()
            if app.busy or app.result is None: raise AssertionError(app.status.get())
            return app.result
        first = run()
        assert (first["regular_hands"], first["splash_hands"]) == (1, 1)
        assert [v.get() for v in app.metrics] == ["1", "1", "0"]
        screenshots = []
        try:
            from PIL import ImageGrab
            import os
            for tab, filename in [(0, "conversion.png"), (1, "splash_settings.png")]:
                app.notebook.select(tab)
                app.root.update()
                time.sleep(.1)
                image = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
                image.save(destination/filename)
                screenshots.append(filename)
        except ImportError:
            pass
        app.notebook.select(0)
        app.mode.set("new")
        second = run()
        assert second["exported_hands"] == 0 and second["previously_exported_skipped"] == 2
        app.rule_bb.set("1.00")
        app.rule_shared.set("80")
        app.rule_since.set("2026-11-01")
        app.upsert_rule()
        assert len(app.rules) == 6
        assert app.save_profile(show=False)
        from tkinter import ttk
        def buttons(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button): yield child
                yield from buttons(child)
        for size in ("1000x800", "860x690"):
            app.root.geometry(size)
            for tab in (0, 1, 2):
                app.notebook.select(tab)
                app.root.update()
                for button in buttons(app.root):
                    if button.winfo_ismapped():
                        bottom = button.winfo_rooty() + button.winfo_height()
                        right = button.winfo_rootx() + button.winfo_width()
                        assert bottom <= app.root.winfo_rooty() + app.root.winfo_height(), (size, tab, button.cget("text"), "bottom")
                        assert right <= app.root.winfo_rootx() + app.root.winfo_width(), (size, tab, button.cget("text"), "right")
        app.close()
        loaded = Application(data_dir=base/"settings")
        loaded.root.update()
        assert loaded.hero.get() == "TestPlayer" and len(loaded.rules) == 6
        assert loaded.mode.get() == "new"
        loaded.close()
        result = {"gui_flow": "PASS", "first_export": first["exported_hands"], "new_export": second["exported_hands"],
                  "unicode_input_and_output_paths": "PASS", "profile_persistence": "PASS", "button_visibility_1000x800_and_860x690": "PASS", "screenshots": screenshots,
                  "platform": sys.platform, "native_windows": "NOT VERIFIED"}
        (destination/"gui_flow.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(json.dumps(result))


if __name__ == "__main__": main()
