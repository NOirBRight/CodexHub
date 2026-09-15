import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import System from 'system';

const lab = GLib.getenv('CODEXHUB_TRAY_LAB') || '/tmp/codexhub-tray-lab';
const bus = Gio.DBusConnection.new_for_address_sync(
  `unix:path=${lab}/runtime/bus`,
  Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
  null,
  null
);

function call(dest, path, iface, method, params, timeout = 3000) {
  const result = bus.call_sync(dest, path, iface, method, params, null, Gio.DBusCallFlags.NONE, timeout, null);
  if (!result) throw new Error(`no reply from ${dest} ${path} ${method}`);
  return result.deep_unpack();
}

function labelsFromLayout(node) {
  const out = [];
  const walk = (item) => {
    if (!Array.isArray(item) || item.length < 3) return;
    const props = item[1] || {};
    if (Object.prototype.hasOwnProperty.call(props, 'label')) out.push(String(props.label));
    for (const child of item[2] || []) walk(child);
  };
  walk(node);
  return out;
}

function parseItem(item) {
  const text = String(item);
  const at = text.indexOf('@');
  if (at <= 0) return null;
  return {dest: text.slice(0, at), path: `${text.slice(at + 1)}/Menu`};
}

const layouts = [];
let items = [];
try {
  const packed = call(
    'org.kde.StatusNotifierWatcher',
    '/StatusNotifierWatcher',
    'org.freedesktop.DBus.Properties',
    'Get',
    new GLib.Variant('(ss)', ['org.kde.StatusNotifierWatcher', 'RegisteredStatusNotifierItems'])
  )[0];
  items = packed.deep_unpack ? packed.deep_unpack() : packed;
} catch (e) {
  items = [];
  layouts.push({error: `watcher: ${e}`});
}

for (const item of items) {
  const parsed = parseItem(item);
  if (!parsed) continue;
  try {
    const layout = call(parsed.dest, parsed.path, 'com.canonical.dbusmenu', 'GetLayout', new GLib.Variant('(iias)', [0, -1, []]));
    layouts.push({
      dest: parsed.dest,
      path: parsed.path,
      revision: layout[0],
      labels: labelsFromLayout(layout[1]),
    });
  } catch (e) {
    layouts.push({dest: parsed.dest, path: parsed.path, error: String(e)});
  }
}

print(JSON.stringify({items, layouts}, null, 2));
System.exit(0);
