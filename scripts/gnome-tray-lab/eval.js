import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
const lab = GLib.getenv('CODEXHUB_TRAY_LAB') || '/tmp/codexhub-tray-lab';
const bus = Gio.DBusConnection.new_for_address_sync(
  `unix:path=${lab}/runtime/bus`,
  Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
  null,
  null
);
const result = bus.call_sync(
  'org.gnome.Shell',
  '/org/gnome/Shell',
  'org.gnome.Shell',
  'Eval',
  new GLib.Variant('(s)', [ARGV[0]]),
  null,
  Gio.DBusCallFlags.NONE,
  15000,
  null
).deep_unpack();
if (!result[0]) throw new Error(result[1]);
let value = JSON.parse(result[1]);
if (typeof value === 'string') {
  try { value = JSON.parse(value); } catch {}
}
print(JSON.stringify(value, null, 2));
