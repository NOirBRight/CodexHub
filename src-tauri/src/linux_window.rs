//! Linux window shell: stay on the taskbar, keep the full GTK pointer-input
//! region, and reconcile the single user launcher used by AppImage installs.

use gtk::gdk::{self, WindowTypeHint};
use gtk::prelude::*;
use std::cell::Cell;
use std::io::Write;
use std::rc::Rc;
use std::time::Duration;
use tauri::{Manager, WebviewWindow};

const APP_ICON_PNG: &[u8] = include_bytes!("../icons/128x128.png");
const APPIMAGE_DESKTOP_FILE: &str = "com.codexhub.app.desktop";
const LEGACY_DESKTOP_FILE: &str = "codexhub.desktop";
const SHELL_WINDOW_WIDGET_NAME: &str = "codexhub-main";
const SHELL_WINDOW_CSS: &str = "#codexhub-main { background-color: transparent; }";

/// Set the identity used by GTK/X11 window managers before Tauri creates the
/// application window.  Ubuntu matches a running window to its desktop entry
/// using the WM_CLASS class field; doing this in `setup` is too late because
/// GTK has already created the native window by then.
pub fn initialize_identity() {
    // Register identity before the compositor first sees the window.
    install_hicolor_icon();
    install_desktop_entry();
    if !gtk::is_initialized() {
        if let Err(error) = gtk::init() {
            log::warn!("Linux GTK identity initialization skipped: {error}");
            return;
        }
    }
    glib::set_prgname(Some("com.codexhub.app"));
    glib::set_application_name("CodexHub");
    gdk::set_program_class("com.codexhub.app");
}

pub fn install(app: &tauri::App) {
    let Some(window) = app.get_webview_window("main") else {
        log::warn!("Linux window shell skipped: main window missing");
        return;
    };
    if let Err(error) = configure_shell(&window) {
        log::warn!("Linux taskbar hint failed: {error}");
    }
}

pub fn reveal_on_taskbar(window: &WebviewWindow) {
    let _ = window.set_skip_taskbar(false);
    if let Ok(gtk_window) = window.gtk_window() {
        gtk_window.set_skip_taskbar_hint(false);
        gtk_window.set_skip_pager_hint(false);
        gtk_window.set_type_hint(WindowTypeHint::Normal);
        apply_full_input_region(&gtk_window.clone().upcast());
    }
}

fn configure_shell(window: &WebviewWindow) -> Result<(), String> {
    let gtk_window = window
        .gtk_window()
        .map_err(|error| format!("gtk window: {error}"))?;
    gtk_window.set_skip_taskbar_hint(false);
    gtk_window.set_skip_pager_hint(false);
    gtk_window.set_type_hint(WindowTypeHint::Normal);
    gtk_window.set_accept_focus(true);
    gtk_window.set_app_paintable(true);
    if let Some(screen) = gtk::prelude::WidgetExt::screen(&gtk_window) {
        if let Some(visual) = screen.rgba_visual() {
            gtk_window.set_visual(Some(&visual));
        }
    }
    let _ = window.set_skip_taskbar(false);
    apply_window_icon(&gtk_window);
    gtk_window.set_widget_name(SHELL_WINDOW_WIDGET_NAME);
    apply_shell_window_css(&gtk_window);
    hook_full_input_region(&gtk_window);
    gtk_window.connect_realize(apply_native_outline);
    gtk_window.connect_size_allocate(|window, _| apply_native_outline(window));
    apply_native_outline(&gtk_window);
    fill_webview_on_resize(&gtk_window);
    Ok(())
}

/// Trim the outermost X11 corners while preserving the independent full input
/// region. The RGBA webview draws the panel radius and shadow on both backends.
fn apply_native_outline(window: &gtk::ApplicationWindow) {
    let Some(native) = gtk::prelude::WidgetExt::window(window) else {
        return;
    };
    if !native.display().supports_shapes() {
        return;
    }
    if window.is_maximized() || native.state().contains(gdk::WindowState::FULLSCREEN) {
        native.shape_combine_region(None, 0, 0);
        return;
    }
    if let Ok(region) = rounded_outline(native.width(), native.height()) {
        native.shape_combine_region(Some(&region), 0, 0);
    }
}

fn rounded_outline(width: i32, height: i32) -> Result<cairo::Region, cairo::Error> {
    let width = width.max(1);
    let height = height.max(1);
    let radius = 12.min(width / 2).min(height / 2);
    let region = cairo::Region::create();
    region.union_rectangle(&cairo::RectangleInt::new(
        0,
        radius,
        width,
        height - 2 * radius,
    ))?;
    for y in 0..radius {
        let dy = f64::from(radius - y) - 0.5;
        let inset =
            (f64::from(radius) - (f64::from(radius * radius) - dy * dy).sqrt()).round() as i32;
        for row in [y, height - 1 - y] {
            region.union_rectangle(&cairo::RectangleInt::new(inset, row, width - 2 * inset, 1))?;
        }
    }
    Ok(region)
}

/// WebKitGTK can derive a GTK input shape from page alpha after realize,
/// resize, or redraw. An empty or partial shape sends physical clicks to the
/// window below CodexHub, so restore the full native input region at each of
/// those lifecycle boundaries. This intentionally does not change the visual
/// opaque region.
fn hook_full_input_region(gtk_window: &gtk::ApplicationWindow) {
    let widget: gtk::Widget = gtk_window.clone().upcast();
    apply_full_input_region(&widget);
    widget.connect_realize(apply_full_input_region);
    widget.connect_size_allocate(|widget, _| apply_full_input_region(widget));
    widget.connect_draw(|widget, _| {
        let widget = widget.clone();
        glib::idle_add_local_once(move || apply_full_input_region(&widget));
        glib::Propagation::Proceed
    });
}

fn apply_full_input_region(widget: &gtk::Widget) {
    if widget.has_window() {
        if let Some(gdk_window) = widget.window() {
            let region = cairo::Region::create_rectangle(&cairo::RectangleInt::new(
                0,
                0,
                gdk_window.width().max(1),
                gdk_window.height().max(1),
            ));
            widget.input_shape_combine_region(Some(&region));
            gdk_window.input_shape_combine_region(&region, 0, 0);
        }
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            apply_full_input_region(&child);
        }
    }
}

/// wry/WebKitGTK on Linux can park the webview in a GtkFixed at 1×1 when
/// bounds are unset. Stretch GTK widgets *and* the native Gdk/X11 child so
/// the UI actually paints instead of a blank shell.
fn fill_webview_on_resize(gtk_window: &gtk::ApplicationWindow) {
    gtk_window.connect_size_allocate(|window, _| {
        stretch_webview(window, false);
    });
    let window = gtk_window.clone();
    let attempts = Rc::new(Cell::new(0u32));
    glib::timeout_add_local(Duration::from_millis(50), move || {
        stretch_webview(&window, true);
        let n = attempts.get() + 1;
        attempts.set(n);
        if n < 40 {
            glib::ControlFlow::Continue
        } else {
            glib::ControlFlow::Break
        }
    });
}

fn stretch_webview(gtk_window: &gtk::ApplicationWindow, allocate: bool) {
    let width = gtk_window.allocated_width().max(1);
    let height = gtk_window.allocated_height().max(1);
    for child in gtk_window.children() {
        expand_widget_tree(&child, width, height, allocate);
    }
    resize_native_children(gtk_window, width, height);
}

fn expand_widget_tree(widget: &gtk::Widget, width: i32, height: i32, allocate: bool) {
    widget.set_hexpand(true);
    widget.set_vexpand(true);
    let type_name = widget.type_().name();
    if type_name.contains("WebView") || type_name.contains("Fixed") {
        // A size request is a minimum, not the current allocation.
        // Raising it on every resize prevents the window from shrinking.
        widget.set_size_request(1, 1);
    }
    if let Ok(fixed) = widget.clone().downcast::<gtk::Fixed>() {
        for child in fixed.children() {
            fixed.move_(&child, 0, 0);
            child.set_size_request(1, 1);
            if allocate || child.allocated_width() != width || child.allocated_height() != height {
                child.size_allocate(&gdk::Rectangle::new(0, 0, width, height));
            }
            if let Some(gdk_window) = child.window() {
                gdk_window.move_resize(0, 0, width, height);
            }
        }
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            expand_widget_tree(&child, width, height, allocate);
        }
    }
}

fn resize_native_children(gtk_window: &gtk::ApplicationWindow, width: i32, height: i32) {
    let Some(gdk_window) = gtk::prelude::WidgetExt::window(gtk_window) else {
        return;
    };
    for child in gdk_window.children() {
        child.move_resize(0, 0, width, height);
    }
}

fn apply_window_icon(gtk_window: &gtk::ApplicationWindow) {
    let name = app_icon_name();
    gtk::Window::set_default_icon_name(&name);
    gtk_window.set_icon_name(Some(&name));
    if let Ok(pixbuf) = gdk_pixbuf::Pixbuf::from_read(std::io::Cursor::new(APP_ICON_PNG)) {
        gtk::Window::set_default_icon(&pixbuf);
        gtk_window.set_icon(Some(&pixbuf));
    }
}

fn apply_shell_window_css(gtk_window: &gtk::ApplicationWindow) {
    let provider = gtk::CssProvider::new();
    // Only the main window is transparent. Never style GTK popup/menu windows.
    // CSS inside the webview draws the rounded panel and its alpha shadow.
    if let Err(error) = provider.load_from_data(SHELL_WINDOW_CSS.as_bytes()) {
        log::warn!("Linux shell CSS failed to load: {error}");
        return;
    }
    gtk_window
        .style_context()
        .add_provider(&provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
}

fn install_hicolor_icon() {
    let Some(root) = dirs::data_local_dir().map(|dir| dir.join("icons/hicolor")) else {
        return;
    };
    let current_name = app_icon_name();
    for (size, bytes) in [
        ("32x32", include_bytes!("../icons/32x32.png").as_slice()),
        ("128x128", APP_ICON_PNG),
        (
            "256x256",
            include_bytes!("../icons/128x128@2x.png").as_slice(),
        ),
        ("256x256@2", include_bytes!("../icons/icon.png").as_slice()),
        ("512x512", include_bytes!("../icons/icon.png").as_slice()),
    ] {
        let icon_dir = root.join(size).join("apps");
        if let Err(error) = std::fs::create_dir_all(&icon_dir) {
            log::warn!("failed to create hicolor icon dir: {error}");
            continue;
        }
        for name in ["codexhub", "com.codexhub.app", &current_name] {
            let path = icon_dir.join(format!("{name}.png"));
            if std::fs::read(&path).is_ok_and(|existing| existing == bytes) {
                continue;
            }
            if let Err(error) = std::fs::write(&path, bytes) {
                log::warn!("failed to write {name}: {error}");
            }
        }
    }
}

fn app_icon_name() -> String {
    use sha2::{Digest, Sha256};
    let fingerprint = format!("{:x}", Sha256::digest(APP_ICON_PNG));
    format!("codexhub-{}", &fingerprint[..12])
}

fn install_desktop_entry() {
    let Some(apps_dir) = dirs::data_local_dir().map(|dir| dir.join("applications")) else {
        return;
    };
    let executable = std::env::current_exe().ok();
    let launcher = appimage_path().or_else(|| {
        executable
            .clone()
            .filter(|path| is_portable_executable(path))
    });
    // Only the installed package may retire the managed user launcher.
    // Running cargo/dev must not erase an AppImage or portable identity.
    let packaged = executable.as_ref().is_some_and(|path| {
        std::fs::canonicalize("/usr/bin/codexhub").is_ok_and(|installed| installed == *path)
    });
    if launcher.is_none() && !packaged {
        return;
    }
    if let Err(error) = reconcile_desktop_entries(&apps_dir, launcher.as_deref()) {
        log::warn!("failed to reconcile Linux desktop entry: {error}");
    }
}

fn is_portable_executable(executable: &std::path::Path) -> bool {
    // A portable archive carries these resources beside its executable.
    // Do not replace a package-owned launcher when running /usr/bin/codexhub
    // or a development binary under target/.
    executable.file_name() == Some(std::ffi::OsStr::new("CodexHub"))
        && executable.parent().is_some_and(|directory| {
            directory.join("src-python/codex_proxy.py").is_file()
                && directory.join("config/providers.toml").is_file()
        })
}

fn reconcile_desktop_entries(
    apps_dir: &std::path::Path,
    appimage: Option<&std::path::Path>,
) -> std::io::Result<()> {
    if !apps_dir.exists() {
        if appimage.is_none() {
            return Ok(());
        }
        std::fs::create_dir_all(apps_dir)?;
    }

    let canonical = apps_dir.join(APPIMAGE_DESKTOP_FILE);
    let legacy = apps_dir.join(LEGACY_DESKTOP_FILE);
    archive_managed_desktop_entry(&legacy)?;

    let Some(appimage) = appimage else {
        // deb/rpm packages own their stable system launcher. Remove only files
        // created by older CodexHub runtimes so package upgrades leave one
        // effective launcher without touching user customizations.
        archive_managed_desktop_entry(&canonical)?;
        return Ok(());
    };

    if canonical.is_file() {
        let existing = std::fs::read_to_string(&canonical)?;
        if !is_managed_desktop_entry(&existing) {
            return Ok(());
        }
    }

    let exec = quote_desktop_exec(appimage);
    // A FileIcon bypasses stale positive/negative icon-theme lookups in an
    // already-running GNOME session. The file lives outside the portable folder.
    let icon = apps_dir
        .parent()
        .unwrap_or(apps_dir)
        .join("icons/hicolor/128x128/apps")
        .join(format!("{}.png", app_icon_name()));
    let icon = icon
        .to_string_lossy()
        .replace('\\', "\\\\")
        .replace('\n', "\\n")
        .replace('\r', "\\r");
    let body = format!(
        "[Desktop Entry]\n\
         Type=Application\n\
         Name=CodexHub\n\
         Comment=CodexHub desktop backend and CLI\n\
         Exec={exec}\n\
         Icon={icon}\n\
         Terminal=false\n\
         Categories=Development;\n\
         StartupNotify=true\n\
         StartupWMClass=com.codexhub.app\n\
         X-GNOME-UsesNotifications=true\n\
         X-CodexHub-Managed=true\n"
    );
    write_if_changed(&canonical, &body)
}

fn appimage_path() -> Option<std::path::PathBuf> {
    std::env::var("APPIMAGE")
        .ok()
        .filter(|path| {
            std::path::Path::new(path)
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.to_ascii_lowercase().starts_with("codexhub"))
        })
        .map(std::path::PathBuf::from)
}

fn quote_desktop_exec(path: &std::path::Path) -> String {
    let raw = path.display().to_string();
    if raw.chars().any(|ch| ch.is_whitespace()) {
        format!("\"{}\"", raw.replace('"', "\\\""))
    } else {
        raw
    }
}

fn archive_managed_desktop_entry(path: &std::path::Path) -> std::io::Result<()> {
    let body = match std::fs::read_to_string(path) {
        Ok(body) => body,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if is_managed_desktop_entry(&body) {
        let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "desktop entry filename is not UTF-8",
            ));
        };
        for index in 0..100 {
            let suffix = if index == 0 {
                String::new()
            } else {
                format!(".{index}")
            };
            let backup = path.with_file_name(format!("{file_name}.codexhub-legacy-backup{suffix}"));
            match std::fs::hard_link(path, &backup) {
                Ok(()) => {
                    std::fs::remove_file(path)?;
                    return Ok(());
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "too many legacy desktop entry backups",
        ));
    }
    Ok(())
}

fn is_managed_desktop_entry(body: &str) -> bool {
    const REQUIRED_LINES: [&str; 9] = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=CodexHub",
        "Comment=CodexHub desktop backend and CLI",
        "Terminal=false",
        "Categories=Development;",
        "StartupNotify=true",
        "StartupWMClass=com.codexhub.app",
        "X-GNOME-UsesNotifications=true",
    ];
    let lines: Vec<&str> = body.lines().collect();
    let managed_marker = lines.contains(&"X-CodexHub-Managed=true");
    let recognized_icon = |line: &str| {
        line == "Icon=codexhub"
            || (managed_marker
                && line.strip_prefix("Icon=codexhub-").is_some_and(|hash| {
                    hash.len() == 12 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
                }))
            || (managed_marker
                && line.strip_prefix("Icon=").is_some_and(|value| {
                    let path = std::path::Path::new(value);
                    path.is_absolute()
                        && path
                            .parent()
                            .is_some_and(|p| p.ends_with("icons/hicolor/128x128/apps"))
                        && path
                            .file_name()
                            .and_then(|name| name.to_str())
                            .and_then(|name| name.strip_prefix("codexhub-"))
                            .and_then(|name| name.strip_suffix(".png"))
                            .is_some_and(|hash| {
                                hash.len() == 12
                                    && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
                            })
                }))
    };
    if !matches!(lines.len(), 11 | 12)
        || lines.iter().filter(|line| recognized_icon(line)).count() != 1
        || !REQUIRED_LINES
            .iter()
            .all(|required| lines.contains(required))
        || lines
            .iter()
            .filter(|line| line.starts_with("Exec=") && line.len() > "Exec=".len())
            .count()
            != 1
    {
        return false;
    }
    let known_lines = lines.iter().all(|line| {
        REQUIRED_LINES.contains(line)
            || recognized_icon(line)
            || (line.starts_with("Exec=") && line.len() > "Exec=".len())
            || *line == "X-CodexHub-Managed=true"
    });
    let legacy_exec = lines
        .iter()
        .find_map(|line| line.strip_prefix("Exec="))
        .is_some_and(is_legacy_managed_exec);
    known_lines && (managed_marker || (lines.len() == 11 && legacy_exec))
}

fn is_legacy_managed_exec(raw: &str) -> bool {
    let command = raw
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .unwrap_or(raw);
    let lower = command.to_ascii_lowercase();
    if lower == "/usr/bin/codexhub"
        || (lower.contains("/src-tauri/target/") && lower.ends_with("/codexhub"))
    {
        return true;
    }
    std::path::Path::new(&lower)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("codexhub") && name.ends_with(".appimage"))
}

fn write_if_changed(path: &std::path::Path, body: &str) -> std::io::Result<()> {
    if std::fs::read_to_string(path).ok().as_deref() == Some(body) {
        return Ok(());
    }
    let mut file = std::fs::File::create(path)?;
    file.write_all(body.as_bytes())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        reconcile_desktop_entries, APPIMAGE_DESKTOP_FILE, LEGACY_DESKTOP_FILE, SHELL_WINDOW_CSS,
        SHELL_WINDOW_WIDGET_NAME,
    };
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn portable_launcher_is_loadable_and_uses_the_running_executable() {
        use std::os::unix::fs::PermissionsExt;
        let root = TestDir::new("portable-identity");
        let portable = root.path().join("portable with spaces");
        fs::create_dir_all(portable.join("src-python")).unwrap();
        fs::create_dir_all(portable.join("config")).unwrap();
        fs::write(portable.join("src-python/codex_proxy.py"), "").unwrap();
        fs::write(portable.join("config/providers.toml"), "").unwrap();
        let executable = portable.join("CodexHub");
        fs::write(
            &executable,
            "#!/bin/sh\ntouch \"$(dirname \"$0\")/launched\"\n",
        )
        .unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(super::is_portable_executable(&executable));
        // Cargo also copies resources beside its lowercase development binary.
        assert!(!super::is_portable_executable(&portable.join("codexhub")));
        assert!(!super::is_portable_executable(
            &root.path().join("target/CodexHub")
        ));
        let apps = root.path().join("applications");
        reconcile_desktop_entries(&apps, Some(&executable)).unwrap();
        let entry = gtk::gio::DesktopAppInfo::from_filename(apps.join(APPIMAGE_DESKTOP_FILE))
            .expect("GNOME must be able to load the portable launcher");
        use gtk::gio::prelude::*;
        entry
            .launch(&[], None::<&gtk::gio::AppLaunchContext>)
            .unwrap();
        for _ in 0..100 {
            if portable.join("launched").is_file() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert!(
            portable.join("launched").is_file(),
            "desktop launch must resolve the full path"
        );
        assert_eq!(
            gtk::gio::prelude::IconExt::to_string(&entry.icon().unwrap()).unwrap(),
            root.path()
                .join("icons/hicolor/128x128/apps")
                .join(format!("{}.png", super::app_icon_name()))
                .to_string_lossy()
                .as_ref()
        );
    }

    #[test]
    fn candidate_upgrade_preserves_gnome_launcher_and_icon() {
        let root = TestDir::new("candidate-icon-upgrade");
        let old = root.path().join("old/CodexHub");
        let new = root.path().join("new/CodexHub");
        for executable in [&old, &new] {
            fs::create_dir_all(executable.parent().unwrap()).unwrap();
            fs::write(executable, "#!/bin/sh\nexit 0\n").unwrap();
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
        }
        reconcile_desktop_entries(root.path(), Some(&old)).unwrap();
        reconcile_desktop_entries(root.path(), Some(&new)).unwrap();
        let entry =
            gtk::gio::DesktopAppInfo::from_filename(root.path().join(APPIMAGE_DESKTOP_FILE))
                .expect("GNOME must resolve the upgraded candidate");
        use gtk::gio::prelude::*;
        assert_eq!(entry.executable(), new);
        assert_eq!(
            gtk::gio::prelude::IconExt::to_string(&entry.icon().unwrap()).unwrap(),
            root.path()
                .parent()
                .unwrap()
                .join("icons/hicolor/128x128/apps")
                .join(format!("{}.png", super::app_icon_name()))
                .to_string_lossy()
                .as_ref()
        );
    }

    #[test]
    fn native_outline_clips_corners_but_preserves_edges_and_content() {
        let region = super::rounded_outline(820, 620).expect("valid outline");
        for (x, y) in [(0, 0), (819, 0), (0, 619), (819, 619)] {
            assert!(!region.contains_point(x, y));
        }
        for (x, y) in [(410, 0), (0, 310), (819, 310), (410, 619), (12, 12)] {
            assert!(region.contains_point(x, y));
        }
        assert!(super::rounded_outline(1, 1).unwrap().contains_point(0, 0));
    }

    struct TestDir(PathBuf);

    impl TestDir {
        fn new(name: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock should be after epoch")
                .as_nanos();
            let path = std::env::temp_dir().join(format!(
                "codexhub-linux-window-{name}-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir_all(&path).expect("create test dir");
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn legacy_generated_entry(exec: &str) -> String {
        format!(
            "[Desktop Entry]\n\
             Type=Application\n\
             Name=CodexHub\n\
             Comment=CodexHub desktop backend and CLI\n\
             Exec={exec}\n\
             Icon=codexhub\n\
             Terminal=false\n\
             Categories=Development;\n\
             StartupNotify=true\n\
             StartupWMClass=com.codexhub.app\n\
             X-GNOME-UsesNotifications=true\n"
        )
    }

    #[test]
    fn shell_window_css_is_exactly_scoped_to_the_named_main_window() {
        assert_eq!(SHELL_WINDOW_WIDGET_NAME, "codexhub-main");
        assert_eq!(
            SHELL_WINDOW_CSS,
            "#codexhub-main { background-color: transparent; }"
        );
    }

    #[test]
    fn shell_window_css_provider_stays_window_scoped() {
        let production_source = include_str!("linux_window.rs")
            .split("\n#[cfg(test)]")
            .next()
            .expect("source contains the production module");
        assert!(production_source.contains("set_widget_name(SHELL_WINDOW_WIDGET_NAME)"));
        assert!(production_source.contains(".style_context()"));
        assert!(production_source
            .contains(".add_provider(&provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION)"));
        let screen_provider = ["add_provider", "for_screen"].join("_");
        let bare_window_selector = ["window", " {"].concat();
        let universal_selector = ["*", " {"].concat();
        assert!(!production_source.contains(&screen_provider));
        assert!(!production_source.contains(&bare_window_selector));
        assert!(!production_source.contains(&universal_selector));
    }

    #[test]
    fn packaged_install_removes_both_runtime_generated_launchers() {
        let root = TestDir::new("package-upgrade");
        for name in [APPIMAGE_DESKTOP_FILE, LEGACY_DESKTOP_FILE] {
            fs::write(
                root.path().join(name),
                legacy_generated_entry("/usr/bin/codexhub"),
            )
            .expect("seed legacy launcher");
        }

        reconcile_desktop_entries(root.path(), None).expect("reconcile package launchers");

        assert!(!root.path().join(APPIMAGE_DESKTOP_FILE).exists());
        assert!(!root.path().join(LEGACY_DESKTOP_FILE).exists());
        assert!(root
            .path()
            .join(format!("{APPIMAGE_DESKTOP_FILE}.codexhub-legacy-backup"))
            .exists());
        assert!(root
            .path()
            .join(format!("{LEGACY_DESKTOP_FILE}.codexhub-legacy-backup"))
            .exists());
    }

    #[test]
    fn packaged_install_preserves_a_user_customized_launcher() {
        let root = TestDir::new("custom-launcher");
        let custom = legacy_generated_entry("/custom/codexhub");
        fs::write(root.path().join(LEGACY_DESKTOP_FILE), &custom).expect("seed custom launcher");

        reconcile_desktop_entries(root.path(), None).expect("reconcile package launchers");

        assert_eq!(
            fs::read_to_string(root.path().join(LEGACY_DESKTOP_FILE))
                .expect("read custom launcher"),
            custom
        );
    }

    #[test]
    fn appimage_upgrade_rewrites_one_stable_launcher_and_removes_the_alias() {
        let root = TestDir::new("appimage-upgrade");
        fs::write(
            root.path().join(APPIMAGE_DESKTOP_FILE),
            legacy_generated_entry("/downloads/CodexHub-old.AppImage"),
        )
        .expect("seed old canonical launcher");
        fs::write(
            root.path().join(LEGACY_DESKTOP_FILE),
            legacy_generated_entry("/downloads/CodexHub-old.AppImage"),
        )
        .expect("seed old launcher alias");

        reconcile_desktop_entries(
            root.path(),
            Some(Path::new("/downloads/CodexHub-new.AppImage")),
        )
        .expect("reconcile appimage launcher");

        let launcher = fs::read_to_string(root.path().join(APPIMAGE_DESKTOP_FILE))
            .expect("read canonical launcher");
        assert!(launcher.contains("Exec=/downloads/CodexHub-new.AppImage"));
        assert!(launcher.contains("X-CodexHub-Managed=true"));
        assert!(!root.path().join(LEGACY_DESKTOP_FILE).exists());
    }

    #[test]
    fn unreadable_legacy_launcher_fails_closed_instead_of_silently_duplicating() {
        let root = TestDir::new("invalid-legacy-launcher");
        fs::write(root.path().join(LEGACY_DESKTOP_FILE), [0xff, 0xfe])
            .expect("seed invalid launcher");

        let error = reconcile_desktop_entries(root.path(), None)
            .expect_err("invalid launcher must fail reconciliation");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert!(root.path().join(LEGACY_DESKTOP_FILE).exists());
    }
}
