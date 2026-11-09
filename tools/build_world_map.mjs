// Builds web/world-map.js: country outlines as SVG paths (Natural Earth 1:110m via world-atlas).
// The app embeds the result in web/index.html, because its Content-Security-Policy allows no
// external downloads. Regenerate with:
//   npm install world-atlas@2.0.2 topojson-client@3.1.0 d3-geo@3.1.1
//   node tools/build_world_map.mjs > /tmp/world.js
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {feature} from 'topojson-client';
import {geoNaturalEarth1, geoPath} from 'd3-geo';

const require = createRequire(import.meta.url);
const topo = JSON.parse(readFileSync(require.resolve('world-atlas/countries-110m.json'), 'utf8'));
const countries = feature(topo, topo.objects.countries).features.filter(f => f.properties.name !== 'Antarctica');
const projection = geoNaturalEarth1().fitExtent([[4, 4], [956, 496]], {type: 'FeatureCollection', features: countries});
const path = geoPath(projection).digits(0);
const out = {};
for (const f of countries) {
  const d = path(f);
  if (d) out[f.properties.name] = d.replace(/Z(?=M)/g, 'Z');
}
// Small places missing at this scale: drawn as dots.
const dots = {Singapore: [103.82, 1.35], 'Hong Kong': [114.17, 22.32], Macau: [113.55, 22.2], Luxembourg: [6.13, 49.61], Malta: [14.51, 35.9], Bahrain: [50.56, 26.07], Monaco: [7.42, 43.74], Andorra: [1.52, 42.51], Liechtenstein: [9.55, 47.14], Maldives: [73.5, 4.17], Mauritius: [57.55, -20.35], Barbados: [-59.54, 13.19]};
const points = Object.fromEntries(Object.entries(dots).map(([k, ll]) => [k, projection(ll).map(Math.round)]));
process.stdout.write('const WORLD_MAP = ' + JSON.stringify({paths: out, dots: points}) + ';\n');
