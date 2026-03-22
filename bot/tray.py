from __future__ import annotations

import logging
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.app_paths import AppPaths, get_app_paths
from bot.launchd import (
    bootout_launch_agent,
    bootstrap_launch_agent,
    is_launch_agent_enabled,
    is_launch_agent_loaded,
    kickstart_launch_agent,
    set_launch_agent_enabled,
)


def main() -> int:
    paths = get_app_paths()
    if paths.service_kind != "launchd":
        raise SystemExit("Tray helper is only available on macOS.")

    try:
        import objc  # type: ignore
        from AppKit import (  # type: ignore
            NSApplication,
            NSApplicationActivationPolicyAccessory,
            NSImage,
            NSMenu,
            NSMenuItem,
            NSObject,
            NSStatusBar,
            NSVariableStatusItemLength,
        )
        from Foundation import NSMakeSize, NSTimer  # type: ignore
    except Exception as exc:  # pragma: no cover - import/runtime guard
        raise SystemExit(f"Tray helper requires PyObjC on macOS: {exc}") from exc

    _configure_logging(paths)

    def make_item(target: object, title: str, action: str):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        item.setTarget_(target)
        return item

    class TrayController(NSObject):  # type: ignore[misc]
        def init(self) -> TrayController | None:  # type: ignore[override]
            self = objc.super(TrayController, self).init()
            if self is None:
                return None
            self.paths = paths
            self.status_item = None
            self.menu = None
            self.status_menu_item = None
            self.toggle_bot_menu_item = None
            self.restart_menu_item = None
            self.launch_item = None
            self.icon_path = Path(__file__).resolve().parent / "assets" / "tray_icon.png"
            return self

        def applicationDidFinishLaunching_(self, notification) -> None:
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

            self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            button = self.status_item.button()
            if self.icon_path.exists():
                image = NSImage.alloc().initWithContentsOfFile_(str(self.icon_path))
                if image is not None:
                    image.setTemplate_(True)
                    image.setSize_(NSMakeSize(18, 18))
                    button.setImage_(image)
            else:
                button.setTitle_("ex")
            button.setToolTip_("ex-cod-tg")

            self.menu = NSMenu.alloc().init()

            title_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("ex-cod-tg", None, "")
            title_item.setEnabled_(False)
            self.menu.addItem_(title_item)

            self.status_menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Status: Checking…", None, "")
            self.status_menu_item.setEnabled_(False)
            self.menu.addItem_(self.status_menu_item)

            self.menu.addItem_(NSMenuItem.separatorItem())

            self.toggle_bot_menu_item = make_item(self, "Start bot", "toggleBot:")
            self.restart_menu_item = make_item(self, "Restart bot", "restartBot:")
            self.menu.addItem_(self.toggle_bot_menu_item)
            self.menu.addItem_(self.restart_menu_item)

            self.menu.addItem_(NSMenuItem.separatorItem())

            self.menu.addItem_(make_item(self, "Open logs", "openLogs:"))
            self.launch_item = make_item(self, "Launch at login", "toggleLaunchAtLogin:")
            self.menu.addItem_(self.launch_item)

            self.menu.addItem_(NSMenuItem.separatorItem())
            self.menu.addItem_(make_item(self, "Quit", "quitApp:"))

            self.status_item.setMenu_(self.menu)
            self.refreshStatus_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3.0, self, "refreshStatus:", None, True)

        def refreshStatus_(self, sender) -> None:
            running = is_launch_agent_loaded(self.paths.service_label)
            self.status_menu_item.setTitle_(f"Status: {'Running' if running else 'Stopped'}")
            if self.toggle_bot_menu_item is not None:
                self.toggle_bot_menu_item.setTitle_("Stop bot" if running else "Start bot")
            self.restart_menu_item.setEnabled_(running)
            if self.launch_item is not None:
                enabled = is_launch_agent_enabled(self.paths.service_label)
                if self.paths.helper_service_label:
                    enabled = enabled and is_launch_agent_enabled(self.paths.helper_service_label)
                self.launch_item.setState_(1 if enabled else 0)

        def toggleBot_(self, sender) -> None:
            if is_launch_agent_loaded(self.paths.service_label):
                bootout_launch_agent(self.paths.service_file)
            else:
                bootstrap_launch_agent(self.paths.service_file)
                kickstart_launch_agent(self.paths.service_label, check=False)
            self.refreshStatus_(None)

        def restartBot_(self, sender) -> None:
            if is_launch_agent_loaded(self.paths.service_label):
                kickstart_launch_agent(self.paths.service_label, check=False)
            else:
                bootstrap_launch_agent(self.paths.service_file)
                kickstart_launch_agent(self.paths.service_label, check=False)
            self.refreshStatus_(None)

        def openLogs_(self, sender) -> None:
            log_target = str(self.paths.log_file)
            try:
                subprocess.run(["open", "-a", "Console", log_target], check=False)
            except FileNotFoundError:
                subprocess.run(["open", log_target], check=False)

        def toggleLaunchAtLogin_(self, sender) -> None:
            currently_enabled = bool(self.launch_item.state())
            next_enabled = not currently_enabled
            set_launch_agent_enabled(self.paths.service_label, enabled=next_enabled)
            if self.paths.helper_service_label:
                set_launch_agent_enabled(self.paths.helper_service_label, enabled=next_enabled)
            self.refreshStatus_(None)

        def quitApp_(self, sender) -> None:
            bootout_launch_agent(self.paths.service_file)
            NSApplication.sharedApplication().terminate_(None)

    app = NSApplication.sharedApplication()
    delegate = TrayController.alloc().init()
    app.setDelegate_(delegate)
    app.run()
    return 0


def _configure_logging(paths: AppPaths) -> None:
    log_file = paths.helper_log_file or paths.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


if __name__ == "__main__":
    raise SystemExit(main())
