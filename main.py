"""Desktop entry point. --convert and --smoke-test enable reproducible checks."""
import argparse
import json
import sys
from pathlib import Path

# The portable Windows package contains its own Tcl/Tk and interpreter.
runtime = Path(__file__).resolve().parent / "runtime"
if sys.platform == "win32" and (runtime / "tcl").is_dir():
    import os
    os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--convert", nargs="+", metavar="INPUT")
    parser.add_argument("--hero", default="Hero")
    parser.add_argument("--output")
    parser.add_argument("--history")
    parser.add_argument("--mode", choices=["all", "new"], default="all")
    parser.add_argument("--settings")
    parser.add_argument("--result-json")
    parser.add_argument("--smoke-test", metavar="REPORT_JSON")
    args = parser.parse_args()
    if args.convert:
        from app.engine import Options, app_data_dir, convert
        options = Options(args.hero, args.mode)
        if args.settings: options.rules = json.loads(Path(args.settings).read_text("utf-8-sig"))["rules"]
        result = convert([Path(p) for p in args.convert], Path(args.output or "converted"), options,
                         Path(args.history) if args.history else app_data_dir()/"history.sqlite")
        if args.result_json: Path(args.result_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        elif sys.stdout: print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.smoke_test:
        import tempfile
        import platform
        from zoneinfo import ZoneInfo
        from app.gui import Application
        with tempfile.TemporaryDirectory() as temp:
            app = Application(data_dir=Path(temp))
            app.root.update()
            app.hero.set("TestPlayer")
            app.save_profile(show=False)
            result = {"platform": platform.platform(), "python": sys.version, "gui_created": True,
                      "rules_count": len(app.rule_table.get_children()), "timezone": str(ZoneInfo("America/New_York")),
                      "settings_saved": app.settings_file.exists()}
            app.close()
            Path(args.smoke_test).write_text(json.dumps(result, indent=2), "utf-8")
    else:
        from app.gui import main as gui_main
        gui_main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        import tempfile
        import os
        detail = traceback.format_exc()
        target = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "CoinPokerConverter" / "startup-error.txt"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(detail, "utf-8")
        except OSError:
            target = Path(tempfile.gettempdir()) / "CoinPokerConverter-error.txt"
            target.write_text(detail, "utf-8")
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "Не удалось запустить конвертер. Подробности сохранены:\n" + str(target), "CoinPoker Converter", 0x10)
        elif sys.stderr:
            print(detail, file=sys.stderr)
        raise SystemExit(1)
