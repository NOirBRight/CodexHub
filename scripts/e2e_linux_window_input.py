"""Physical Linux pointer-input regression for the real CodexHub window."""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import ctypes
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


def find_codexhub_window() -> int | None:
    result = subprocess.run(
        ["xwininfo", "-root", "-tree"],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    matches = re.findall(
        r'^\s*(0x[0-9a-f]+)\s+"CodexHub"(?::|\s)',
        result.stdout,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    candidates: list[tuple[int, int, int]] = []
    for match in matches:
        window_id = int(match, 16)
        try:
            _, _, width, height = window_geometry(window_id)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            continue
        candidates.append((window_id, width, height))
    return select_interactive_window(candidates)


def select_interactive_window(candidates: list[tuple[int, int, int]]) -> int | None:
    # AppIndicator also exposes a 16x16 window called CodexHub. Treat it as
    # infrastructure, not the interactive application under test.
    interactive = [
        (width * height, window_id)
        for window_id, width, height in candidates
        if width >= 320 and height >= 240
    ]
    return max(interactive)[1] if interactive else None


def window_geometry(window_id: int) -> tuple[int, int, int, int]:
    result = subprocess.run(
        ["xwininfo", "-id", f"0x{window_id:x}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    values: dict[str, int] = {}
    patterns = {
        "x": r"Absolute upper-left X:\s+(-?\d+)",
        "y": r"Absolute upper-left Y:\s+(-?\d+)",
        "width": r"Width:\s+(\d+)",
        "height": r"Height:\s+(\d+)",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, result.stdout)
        if match is None:
            raise RuntimeError(f"xwininfo omitted {name} for 0x{window_id:x}")
        values[name] = int(match.group(1))
    return values["x"], values["y"], values["width"], values["height"]


class X11Harness:
    SHAPE_INPUT = 2
    BUTTON_PRESS_MASK = 1 << 2

    def __init__(self) -> None:
        self.x11 = ctypes.CDLL("libX11.so.6")
        self.xtst = ctypes.CDLL("libXtst.so.6")
        self.xext = ctypes.CDLL("libXext.so.6")
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XFree.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XCreateSimpleWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self.x11.XCreateSimpleWindow.restype = ctypes.c_ulong
        self.x11.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
        self.x11.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
        self.x11.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XPending.argtypes = [ctypes.c_void_p]
        self.x11.XPending.restype = ctypes.c_int
        self.x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x11.XGetImage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        self.x11.XGetImage.restype = ctypes.c_void_p
        self.x11.XGetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.x11.XGetPixel.restype = ctypes.c_ulong
        self.x11.XDestroyImage.argtypes = [ctypes.c_void_p]
        self.x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XMoveResizeWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.xtst.XTestFakeMotionEvent.restype = ctypes.c_int
        self.xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.xtst.XTestFakeButtonEvent.restype = ctypes.c_int
        self.xext.XShapeGetRectangles.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.xext.XShapeGetRectangles.restype = ctypes.POINTER(XRectangle)
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("could not open the X11 display")

    def close(self) -> None:
        if self.display:
            self.x11.XCloseDisplay(self.display)
            self.display = None

    def sync(self) -> None:
        self.x11.XSync(self.display, 0)

    def root_window(self) -> int:
        return self.x11.XDefaultRootWindow(self.display)

    def create_probe_window(self, x: int, y: int, width: int, height: int) -> int:
        root = self.x11.XDefaultRootWindow(self.display)
        window = self.x11.XCreateSimpleWindow(
            self.display,
            root,
            x,
            y,
            width,
            height,
            0,
            0,
            0x557799,
        )
        if not window:
            raise RuntimeError("could not create the X11 background probe")
        self.x11.XStoreName(self.display, window, b"CodexHub Input Probe")
        self.x11.XSelectInput(self.display, window, self.BUTTON_PRESS_MASK)
        self.x11.XMapRaised(self.display, window)
        self.sync()
        return window

    def destroy_window(self, window_id: int) -> None:
        self.x11.XDestroyWindow(self.display, window_id)
        self.sync()

    def pending_button_presses(self) -> int:
        count = 0
        event = (ctypes.c_long * 24)()
        self.sync()
        while self.x11.XPending(self.display):
            self.x11.XNextEvent(self.display, ctypes.byref(event))
            count += 1
        return count

    def pointer_target(self) -> tuple[int, int, int]:
        root = self.x11.XDefaultRootWindow(self.display)
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        window_x = ctypes.c_int()
        window_y = ctypes.c_int()
        mask = ctypes.c_uint()
        self.x11.XQueryPointer(
            self.display,
            root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(window_x),
            ctypes.byref(window_y),
            ctypes.byref(mask),
        )
        return child_return.value, root_x.value, root_y.value

    def frame_for(self, window_id: int) -> int:
        current = ctypes.c_ulong(window_id)
        while True:
            root = ctypes.c_ulong()
            parent = ctypes.c_ulong()
            children = ctypes.POINTER(ctypes.c_ulong)()
            child_count = ctypes.c_uint()
            ok = self.x11.XQueryTree(
                self.display,
                current,
                ctypes.byref(root),
                ctypes.byref(parent),
                ctypes.byref(children),
                ctypes.byref(child_count),
            )
            if children:
                self.x11.XFree(children)
            if not ok or not parent.value or parent.value == root.value:
                return current.value
            current = parent

    def move_resize_raise(self, window_id: int, x: int, y: int, width: int, height: int) -> None:
        self.x11.XMoveResizeWindow(self.display, window_id, x, y, width, height)
        self.x11.XRaiseWindow(self.display, window_id)
        self.sync()

    def click(self, x: int, y: int) -> None:
        operations = (
            self.xtst.XTestFakeMotionEvent(self.display, 0, x, y, 0),
            self.xtst.XTestFakeButtonEvent(self.display, 1, 1, 0),
            self.xtst.XTestFakeButtonEvent(self.display, 1, 0, 0),
        )
        self.sync()
        time.sleep(0.18)
        if operations != (1, 1, 1):
            raise RuntimeError(f"XTest rejected physical pointer input: {operations}")

    def move_pointer(self, x: int, y: int) -> None:
        if self.xtst.XTestFakeMotionEvent(self.display, 0, x, y, 0) != 1:
            raise RuntimeError("XTest rejected pointer motion")
        self.sync()

    def pixel_sample(
        self,
        window_id: int,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> tuple[int, ...]:
        image = self.x11.XGetImage(
            self.display,
            window_id,
            x,
            y,
            width,
            height,
            ctypes.c_ulong(-1).value,
            2,
        )
        if not image:
            raise RuntimeError("XGetImage could not capture the CodexHub UI")
        pixels: list[int] = []
        try:
            for pixel_y in range(0, height, 3):
                for pixel_x in range(0, width, 3):
                    pixels.append(self.x11.XGetPixel(image, pixel_x, pixel_y))
        finally:
            self.x11.XDestroyImage(image)
        return tuple(pixels)

    def input_rectangles(self, window_id: int) -> list[tuple[int, int, int, int]]:
        count = ctypes.c_int()
        ordering = ctypes.c_int()
        rectangles = self.xext.XShapeGetRectangles(
            self.display,
            window_id,
            self.SHAPE_INPUT,
            ctypes.byref(count),
            ctypes.byref(ordering),
        )
        try:
            return [
                (
                    rectangles[index].x,
                    rectangles[index].y,
                    rectangles[index].width,
                    rectangles[index].height,
                )
                for index in range(count.value)
            ]
        finally:
            if rectangles:
                self.x11.XFree(rectangles)


def isolated_environment(home: Path) -> dict[str, str]:
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env["GDK_BACKEND"] = "x11"
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / "config")
    env["XDG_CACHE_HOME"] = str(home / "cache")
    env["XDG_DATA_HOME"] = str(home / "data")
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["CODEX_HOME"] = str(home / ".codex")
    env["CODEXHUB_RUNTIME_HOME"] = str(home / ".codexhub")
    return env


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def wait_for_stable_pixels(
    x11: X11Harness,
    window_id: int,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    timeout: float = 15.0,
) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout
    previous: tuple[int, ...] | None = None
    stable_frames = 0
    while time.monotonic() < deadline:
        current = x11.pixel_sample(window_id, x, y, width, height)
        if not visual_frame_has_ui_content(current):
            previous = None
            stable_frames = 0
            time.sleep(0.2)
            continue
        if current == previous:
            stable_frames += 1
        else:
            previous = current
            stable_frames = 1
        if stable_frames >= 5:
            return current
        time.sleep(0.2)
    raise RuntimeError(
        "CodexHub UI did not render a non-blank stable five-frame visual baseline"
    )


def visual_frame_has_ui_content(pixels: tuple[int, ...]) -> bool:
    if not pixels:
        return False
    counts: dict[int, int] = {}
    for pixel in pixels:
        counts[pixel] = counts.get(pixel, 0) + 1
    dominant_fraction = max(counts.values()) / len(pixels)
    return len(counts) >= 8 and dominant_fraction <= 0.95


def changed_pixel_fraction(before: tuple[int, ...], after: tuple[int, ...]) -> float:
    if len(before) != len(after) or not before:
        raise RuntimeError("visual samples are empty or have mismatched dimensions")
    return sum(left != right for left, right in zip(before, after, strict=True)) / len(before)


def drawer_cycle_has_required_transitions(
    opened: float,
    closed: float,
    reopened: float,
    reclosed: float,
    *,
    minimum: float = 0.25,
) -> bool:
    return all(fraction >= minimum for fraction in (opened, closed, reopened, reclosed))


@contextmanager
def retrying_temporary_directory(*, prefix: str):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        for attempt in range(10):
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt == 9:
                    raise
                time.sleep(0.2)


def run_probe(binary: Path) -> int:
    x11 = X11Harness()
    background_xid = x11.create_probe_window(100, 90, 820, 620)
    bg_x, bg_y, bg_width, bg_height = window_geometry(background_xid)
    x11.pending_button_presses()
    x11.click(bg_x + bg_width // 2, bg_y + bg_height // 2)
    calibration_clicks = x11.pending_button_presses()
    if calibration_clicks < 1:
        target, pointer_x, pointer_y = x11.pointer_target()
        print(
            "FAIL: probe calibration failed, "
            f"background_clicks={calibration_clicks} "
            f"background=0x{background_xid:x} target=0x{target:x} "
            f"pointer=({pointer_x},{pointer_y}) geometry="
            f"({bg_x},{bg_y},{bg_width},{bg_height})"
        )
        x11.destroy_window(background_xid)
        x11.close()
        return 1

    # WebKit helpers can finish one last cache write immediately after the app
    # exits. Retry cleanup so the E2E neither flakes nor leaks isolated homes.
    with retrying_temporary_directory(prefix="codexhub-window-input-e2e-") as temp_dir:
        home = Path(temp_dir) / "home"
        home.mkdir()
        process = subprocess.Popen(
            [str(binary)],
            cwd=home,
            env=isolated_environment(home),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            app_xid = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.1)
                app_xid = find_codexhub_window()
                if app_xid is not None:
                    break
            if app_xid is None:
                print("FAIL: CodexHub X11 window was not found")
                return 1

            frame = x11.frame_for(app_xid)
            x11.move_resize_raise(frame, bg_x, bg_y, bg_width, bg_height)
            time.sleep(1.0)
            stable_xid = find_codexhub_window()
            if stable_xid is None:
                print(
                    "FAIL: CodexHub window disappeared before pointer testing; "
                    f"process_status={process.poll()}"
                )
                return 1
            if stable_xid != app_xid:
                app_xid = stable_xid
                frame = x11.frame_for(app_xid)
                x11.move_resize_raise(frame, bg_x, bg_y, bg_width, bg_height)
                time.sleep(0.5)
            app_x, app_y, app_width, app_height = window_geometry(app_xid)

            rectangles = x11.input_rectangles(app_xid)
            points = [
                (app_width * x_percent // 100, app_height * y_percent // 100)
                for y_percent in (18, 50, 82)
                for x_percent in (8, 50, 92)
            ]
            uncovered = [
                point
                for point in points
                if not any(
                    left <= point[0] < left + width and top <= point[1] < top + height
                    for left, top, width, height in rectangles
                )
            ]
            if uncovered:
                print(
                    "FAIL: CodexHub input shape does not cover the window; "
                    f"uncovered={uncovered} rectangles={rectangles}"
                )
                return 1

            # Prove WebKit/DOM receives the event, not merely that the GTK shell
            # prevents it from reaching the background. The settings button is
            # anchored 142 physical pixels from the right edge at every size;
            # opening its drawer changes a large, stable region of the UI.
            response_width = min(360, app_width)
            response_x = app_x + app_width - response_width
            response_height = min(480, app_height)
            x11.move_pointer(5, 5)
            time.sleep(0.3)
            before_response = wait_for_stable_pixels(
                x11,
                x11.root_window(),
                response_x,
                app_y,
                response_width,
                response_height,
            )
            x11.pending_button_presses()
            x11.click(
                app_x + max(0, app_width - 142),
                app_y + min(28, app_height - 1),
            )
            if x11.pending_button_presses():
                print("FAIL: settings-button click passed through to the background window")
                return 1
            x11.move_pointer(5, 5)
            after_response = wait_for_stable_pixels(
                x11,
                x11.root_window(),
                response_x,
                app_y,
                response_width,
                response_height,
            )
            opened_fraction = changed_pixel_fraction(before_response, after_response)
            if opened_fraction < 0.25:
                print(
                    "FAIL: settings drawer did not produce the expected large DOM transition; "
                    f"changed_fraction={opened_fraction:.3f}"
                )
                return 1

            # The open drawer renders a full-height backdrop over the left side
            # of this 820px test window. A physical click there must produce a
            # large close transition without reaching the background probe.
            x11.pending_button_presses()
            x11.click(
                app_x + min(100, app_width - 1),
                app_y + min(300, app_height - 1),
            )
            if x11.pending_button_presses():
                print("FAIL: settings-drawer backdrop click passed through to the background window")
                return 1
            x11.move_pointer(5, 5)
            closed_response = wait_for_stable_pixels(
                x11,
                x11.root_window(),
                response_x,
                app_y,
                response_width,
                response_height,
            )
            close_transition = changed_pixel_fraction(after_response, closed_response)

            # Reopen and close the drawer a second time. The page underneath
            # can legitimately finish asynchronous startup work while the
            # first drawer cycle is open, so it need not return pixel-for-pixel
            # to the old baseline. Two complete physical cycles prove the DOM
            # handled both buttons without depending on stale page pixels.
            x11.pending_button_presses()
            x11.click(
                app_x + max(0, app_width - 142),
                app_y + min(28, app_height - 1),
            )
            if x11.pending_button_presses():
                print("FAIL: second settings-button click passed through to the background window")
                return 1
            x11.move_pointer(5, 5)
            reopened_response = wait_for_stable_pixels(
                x11,
                x11.root_window(),
                response_x,
                app_y,
                response_width,
                response_height,
            )
            reopened_transition = changed_pixel_fraction(closed_response, reopened_response)

            x11.pending_button_presses()
            x11.click(
                app_x + min(100, app_width - 1),
                app_y + min(300, app_height - 1),
            )
            if x11.pending_button_presses():
                print("FAIL: second settings-drawer backdrop click passed through to the background window")
                return 1
            x11.move_pointer(5, 5)
            reclosed_response = wait_for_stable_pixels(
                x11,
                x11.root_window(),
                response_x,
                app_y,
                response_width,
                response_height,
            )
            reclosed_transition = changed_pixel_fraction(reopened_response, reclosed_response)
            if not drawer_cycle_has_required_transitions(
                opened_fraction,
                close_transition,
                reopened_transition,
                reclosed_transition,
            ):
                print(
                    "FAIL: settings drawer did not complete two physical DOM cycles; "
                    f"transitions=({opened_fraction:.3f}, {close_transition:.3f}, "
                    f"{reopened_transition:.3f}, {reclosed_transition:.3f})"
                )
                return 1

            for local_x, local_y in points:
                x11.pending_button_presses()
                x11.click(app_x + local_x, app_y + local_y)
                background_clicks = x11.pending_button_presses()
                if background_clicks:
                    print(
                        "FAIL: CodexHub click passed through to the background window; "
                        f"point=({local_x},{local_y}) background_clicks={background_clicks}"
                    )
                    return 1
                if process.poll() is not None:
                    print(f"FAIL: CodexHub exited during pointer test with {process.returncode}")
                    return 1

            print(
                "PASS: CodexHub settings drawer opened and closed through physical input, "
                "and 13 clicks stayed inside the full input region; "
                f"window={app_width}x{app_height} rectangles={rectangles} "
                f"drawer_transitions=({opened_fraction:.3f}, {close_transition:.3f}, "
                f"{reopened_transition:.3f}, {reclosed_transition:.3f})"
            )
            return 0
        finally:
            terminate_process_group(process)
            time.sleep(0.5)
            x11.destroy_window(background_xid)
            x11.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        selected = select_interactive_window([(0x10, 16, 16), (0x20, 820, 620)])
        if selected != 0x20 or select_interactive_window([(0x10, 16, 16)]) is not None:
            print("FAIL: interactive main-window selection accepted AppIndicator infrastructure")
            return 1
        if changed_pixel_fraction((1, 2, 3, 4), (1, 9, 8, 4)) != 0.5:
            print("FAIL: visual transition measurement is not deterministic")
            return 1
        if visual_frame_has_ui_content((0xFFFFFF,) * 128):
            print("FAIL: stable blank frames are accepted as interactive UI")
            return 1
        print("PASS: Linux pointer-input E2E self-test")
        return 0
    if args.bin is None:
        parser.error("--bin is required unless --self-test is used")
    binary = args.bin.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error(f"CodexHub binary is not executable: {binary}")
    if not os.environ.get("DISPLAY"):
        parser.error("DISPLAY is required; run this test through xvfb-run")
    return run_probe(binary)


if __name__ == "__main__":
    raise SystemExit(main())
