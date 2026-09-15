import GLib from 'gi://GLib';
import System from 'system';

const data = JSON.parse(new TextDecoder().decode(GLib.file_get_contents(ARGV[0])[1]));

function luminance(hex) {
  const c = [1, 3, 5].map(p => parseInt(hex.slice(p, p + 2), 16) / 255).map(v =>
    v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  );
  return c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
}

function classify(menu) {
  const target = menu.id.includes('codexhub') ? 'Show CodexHub' : 'Show Reference';
  const labels = menu.items.map(i => i.text ?? '');
  const ratios = menu.items.map(i => {
    const a = luminance(i.fg);
    const b = luminance(i.bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
  const empty = labels.some(label => !label);
  const missing = !labels.includes(target);
  const minContrast = ratios.length ? Math.min(...ratios) : 0;
  const lowContrast = ratios.some(ratio => ratio < 4.5);
  let kind = 'ok';
  if (missing && empty) kind = 'empty-labels';
  else if (missing) kind = 'missing-target';
  else if (empty) kind = 'empty-labels';
  else if (lowContrast) kind = 'low-contrast';
  return {
    target,
    pass: kind === 'ok',
    kind,
    labels,
    minContrast,
    styles: menu.items.map(i => i.style ?? null),
  };
}

const reports = Array.isArray(data) ? data.map(classify) : [];
const hasCodex = reports.some(r => r.target === 'Show CodexHub');
const hasRef = reports.some(r => r.target === 'Show Reference');
let ok = reports.length === 2 && hasCodex && hasRef;
for (const report of reports) {
  print(JSON.stringify(report));
  ok = ok && report.pass;
}
if (!hasCodex) print(JSON.stringify({target: 'Show CodexHub', pass: false, kind: 'missing-tray', labels: [], minContrast: 0}));
if (!hasRef) print(JSON.stringify({target: 'Show Reference', pass: false, kind: 'missing-tray', labels: [], minContrast: 0}));
System.exit(ok ? 0 : 1);
