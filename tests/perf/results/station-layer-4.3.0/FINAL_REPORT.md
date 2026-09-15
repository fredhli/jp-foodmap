# Station layer performance: PASS

The optimized station-only layer is performance-equivalent to the map with
stations hidden under the calibrated interaction gate. It is suitable for the
4.3.0 release from a performance perspective.

## Release evidence

- Formal candidate: 8,954 stations; 512,544 bytes raw and 186,897 bytes gzip;
  one immutable request on cold load, zero server requests on warm load.
- Station-only mode made zero railway LOD requests in every navigation.
- Fold inner (932×704, 4× CPU): three-trial cross-page median +2.28%; rAF p95
  delta 0 ms.
- Desktop (1440×900, 4× CPU): three-trial cross-page median +4.42%; rAF p95
  delta 0 ms.
- Fold outer (475×751, 6× CPU), same-page ABBA×3 calibration: six visible and
  six hidden 10-drag trials, while retaining the same loaded 8,954-station
  index and layer listeners. Paired-block median was -3.265%; bootstrap 95%
  interval -6.079% to +0.693%; rAF p95 delta 0 ms. This resolves the large
  scheduler noise seen when comparing separate pages.
- Forced-GC heap delta was 1.55–1.59 MiB; DOM delta is two elements (pane
  canvas and style), well below the 5 MiB / 750-node gates.
- Paired 60-second Fold-outer idle windows: stations visible used 151.060 ms
  TaskDuration versus 261.883 ms hidden (delta -110.823 ms). Both had zero
  DrawFrame/BeginFrame/AnimationFrame events, zero station network requests,
  zero pending rAF, no animations, and no extra interval.

## Optimization applied

The initial DivIcon implementation caused marker/tooltip DOM paint and
compositing work during pans. It was replaced with one 1× CSS-pixel station
canvas, using one cached tunnel-and-train vector for all three 14/18/22 px tiers. Station
labels are painted on the canvas, names still follow the active language, and
desktop hover plus touch tap use a lightweight spatial hit list. The canvas is
allocated only when the z12 station payload becomes usable, so nationwide boot
does not allocate a rendering surface. Path2D and measured label widths are
cached. The station index and payload remain independent of railway LODs.

## Raw artifacts

- `result.json`: three viewports, cold/warm starts, three 10-drag trials per
  side, heap/DOM, 15-second idle traces and network records.
- `paired-fold-outer.json`: 12 raw same-page Fold-outer trials and bootstrap
  summary.
- `idle60-paired.json`, `idle60-on.json.gz`, `idle60-off.json.gz`: the final
  60-second energy-proxy measurements and traces.

Functional regressions also pass: default state, explicit-off persistence,
one-way railway coupling, retry/accessibility, payload contract, and the
station layer unit contract.
